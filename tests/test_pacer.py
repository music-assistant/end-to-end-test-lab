"""Tests for the capture writer and the pacer rate limit."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from labctl.pacer import CaptureWriter, RateLimiter, pump


def test_capture_writer_follows_contract(tmp_path: Path) -> None:
    """Synthetic input produces the pcm, stamps and json the analyzer expects."""
    writer = CaptureWriter(
        "slim-1",
        tmp_path,
        48000,
        2,
        container_started_wall_ns=5,
        monotonic_ns=_counter(1000, 10),
        wall_ns=_counter(2000, 10),
    )
    meta = json.loads((tmp_path / "slim-1.json").read_text())
    assert meta == {
        "role": "slim-1",
        "sample_rate": 48000,
        "channels": 2,
        "bits": 16,
        "encoding": "s16le",
        "started_wall_ns": 2000,
        "started_monotonic_ns": 1000,
        "container_started_wall_ns": 5,
    }

    pump([b"\x01\x00" * 4, b"\x02\x00" * 2], writer, None)
    writer.close()

    assert (tmp_path / "slim-1.pcm").read_bytes() == b"\x01\x00" * 4 + b"\x02\x00" * 2
    assert (tmp_path / "slim-1.stamps").read_text() == "1010 2010 0 8\n1020 2020 8 4\n"
    meta = json.loads((tmp_path / "slim-1.json").read_text())
    assert meta["bytes_total"] == 12
    assert meta["ended_wall_ns"] == 2030


def test_rate_limit_sleeps_to_real_time(tmp_path: Path) -> None:
    """An unpaced burst is consumed at the byte rate, and a pause restarts the schedule."""
    now = [0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += int(seconds * 1e9)

    def clock() -> int:
        return now[0]

    limiter = RateLimiter(1000, monotonic_ns=clock, sleep=sleep)
    writer = CaptureWriter(
        "slim-1", tmp_path, 48000, 2, container_started_wall_ns=0, monotonic_ns=clock
    )
    pump([b"\x00" * 100] * 3, writer, limiter)
    assert sleeps == [0.1, 0.1]

    # the source stalls for a second, then bursts again
    now[0] += 1_000_000_000
    pump([b"\x00" * 100] * 2, writer, limiter)
    writer.close()
    assert sleeps == [0.1, 0.1, 0.1]


def _counter(start: int, step: int) -> Callable[[], int]:
    """Return a fake clock that advances by step on every call."""
    value = [start - step]

    def tick() -> int:
        value[0] += step
        return value[0]

    return tick
