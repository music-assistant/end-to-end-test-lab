"""Capture writer and pacer: records the PCM a player produces with per-write timestamps."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

READ_BYTES = 4096
# Falling this far behind the byte schedule means the source paused (underrun), so the
# schedule restarts from now instead of letting a burst catch up.
UNDERRUN_NS = 100_000_000


class CaptureWriter:
    """Writes `<role>.pcm`, `<role>.stamps` and `<role>.json` for one player role."""

    def __init__(
        self,
        role: str,
        captures_dir: Path,
        sample_rate: int,
        channels: int,
        *,
        container_started_wall_ns: int | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wall_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        """Open the role's capture files and write the initial json."""
        self._monotonic_ns = monotonic_ns
        self._wall_ns = wall_ns
        self._offset = 0
        captures_dir.mkdir(parents=True, exist_ok=True)
        self._json_path = captures_dir / f"{role}.json"
        self._pcm = (captures_dir / f"{role}.pcm").open("wb")
        self._stamps = (captures_dir / f"{role}.stamps").open("w", encoding="ascii")
        self._meta: dict[str, Any] = {
            "role": role,
            "sample_rate": sample_rate,
            "channels": channels,
            "bits": 16,
            "encoding": "s16le",
            "started_wall_ns": wall_ns(),
            "started_monotonic_ns": monotonic_ns(),
            "container_started_wall_ns": (
                container_started_wall_ns
                if container_started_wall_ns is not None
                else _container_started_wall_ns()
            ),
        }
        self._write_json()

    @property
    def bytes_total(self) -> int:
        """Bytes written so far."""
        return self._offset

    def write(self, data: bytes) -> None:
        """Append data to the capture and stamp the write."""
        self._stamps.write(f"{self._monotonic_ns()} {self._wall_ns()} {self._offset} {len(data)}\n")
        self._pcm.write(data)
        self._offset += len(data)
        # flushed per write so a killed container still leaves a consistent capture
        self._pcm.flush()
        self._stamps.flush()

    def close(self) -> None:
        """Close the files and finalize the json with the totals."""
        self._pcm.close()
        self._stamps.close()
        self._meta["bytes_total"] = self._offset
        self._meta["ended_wall_ns"] = self._wall_ns()
        self._write_json()

    def _write_json(self) -> None:
        """Write the metadata json."""
        self._json_path.write_text(json.dumps(self._meta, indent=2) + "\n", encoding="ascii")


class RateLimiter:
    """Sleeps so bytes are consumed at a fixed rate, like a sound card draining its buffer."""

    def __init__(
        self,
        bytes_per_second: int,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a limiter for the given byte rate."""
        self._bytes_per_second = bytes_per_second
        self._monotonic_ns = monotonic_ns
        self._sleep = sleep
        self._due_ns: int | None = None

    def wait(self, nbytes: int) -> None:
        """Block until the next nbytes are due, then account for them."""
        now = self._monotonic_ns()
        if self._due_ns is None or now - self._due_ns > UNDERRUN_NS:
            self._due_ns = now
        elif self._due_ns > now:
            self._sleep((self._due_ns - now) / 1e9)
        self._due_ns += nbytes * 1_000_000_000 // self._bytes_per_second


def pump(chunks: Iterable[bytes], writer: CaptureWriter, limiter: RateLimiter | None) -> None:
    """Write every chunk through the writer, paced by the limiter when given."""
    for chunk in chunks:
        if limiter is not None:
            limiter.wait(len(chunk))
        writer.write(chunk)


def main(argv: list[str] | None = None) -> None:
    """Run the `lab-pacer` CLI."""
    parser = argparse.ArgumentParser(prog="lab-pacer", description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--dir", required=True, type=Path, help="captures directory")
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument(
        "--input", type=Path, help="FIFO to read, created when missing; stdin when omitted"
    )
    parser.add_argument("--bytes-per-second", type=int, help="pace an unpaced source")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)
    writer = CaptureWriter(args.role, args.dir, args.rate, args.channels)
    limiter = RateLimiter(args.bytes_per_second) if args.bytes_per_second else None
    chunks = _read_fifo(args.input) if args.input else _read_fd(sys.stdin.fileno())
    try:
        pump(chunks, writer, limiter)
    finally:
        writer.close()


def _read_fd(fd: int) -> Iterator[bytes]:
    """Yield reads from fd until EOF."""
    while chunk := os.read(fd, READ_BYTES):
        yield chunk


def _read_fifo(path: Path) -> Iterator[bytes]:
    """Yield reads from the FIFO forever, reopening it whenever its writer closes."""
    if not path.is_fifo():
        os.mkfifo(path)
        # players may run as another user than the pacer
        path.chmod(0o666)
    while True:
        # blocks until a player opens the FIFO for writing
        fd = os.open(path, os.O_RDONLY)
        try:
            yield from _read_fd(fd)
        finally:
            os.close(fd)


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM and SIGINT into a clean exit that finalizes the capture."""
    raise SystemExit(0)


def _container_started_wall_ns() -> int:
    """Wall time PID 1 started, which is when the container started."""
    # field 22 of /proc/<pid>/stat is the start time in clock ticks since boot
    start_ticks = int(Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19])
    uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    boot_wall_ns = time.time_ns() - int(uptime_s * 1e9)
    return boot_wall_ns + start_ticks * 1_000_000_000 // os.sysconf("SC_CLK_TCK")
