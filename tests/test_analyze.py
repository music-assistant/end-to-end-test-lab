"""Audio oracle tests on synthetic captures."""

import asyncio
import heapq
import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from itertools import count
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from scipy.signal import resample_poly

from labctl.analyze import Capture, Manifest
from labctl.config import REPO_ROOT
from labctl.pacer import READ_BYTES, CaptureWriter, RateLimiter, pump
from scenarios.checks.queue import HEAR_LEAD_S, starts_after
from tones.generate import synthesize

MANIFEST = Manifest.load(REPO_ROOT / "tones" / "manifest.json")
BASE_NS = 1_800_000_000 * 10**9
WINDOW_S = 4096 / 48000


def test_single_tone(tmp_path: Path) -> None:
    """A lone tone between unwritten stretches reads as silence, tone, silence."""
    _write(tmp_path, "p", [(1.0, _tone("a1t1", 5.0))], total_s=7.0)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    silence, tone, trailing = capture.timeline()
    assert (silence.kind, tone.kind, trailing.kind) == ("silence", "a1t1", "silence")
    assert tone.t0 - capture.start == pytest.approx(1.0, abs=WINDOW_S)
    assert tone.duration == pytest.approx(5.0, abs=2 * WINDOW_S)
    assert tone.level == pytest.approx(-12.0, abs=0.5)


def test_two_tones_with_gap(tmp_path: Path) -> None:
    """A one second stretch without writes between two tracks is a gap."""
    _write(tmp_path, "p", [(0.5, _tone("a1t1", 3.0)), (4.5, _tone("a1t2", 3.0))], total_s=8.0)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    sounds = [s.kind for s in capture.timeline() if s.kind != "silence"]
    assert sounds == ["a1t1", "a1t2"]
    (gap,) = capture.gaps()
    assert gap.duration == pytest.approx(1.0, abs=2 * WINDOW_S)


def test_load_since_matches_full_load(tmp_path: Path) -> None:
    """A capture loaded from `since` holds only that tail and classifies it like a full load."""
    _write(tmp_path, "p", [(0.5, _tone("a1t1", 3.0)), (4.5, _tone("a1t2", 3.0))], total_s=8.0)
    full = Capture.load(tmp_path, "p", MANIFEST)
    # inside a1t2, so a write chunk straddles the cut
    since = full.start + 5.0
    tail = Capture.load(tmp_path, "p", MANIFEST, since=since)
    assert since - WINDOW_S < tail.start <= since
    first = round((tail.start - full.start) * full.rate)
    assert np.array_equal(tail.samples, full.samples[first:])
    assert [s.kind for s in tail.timeline()] == ["a1t2", "silence"]


def test_first_sound_skips_written_silence(tmp_path: Path) -> None:
    """Silence an idle player writes is not sound; the first sound after `since` is."""
    _write(tmp_path, "p", [(0.0, np.zeros(48000)), (1.0, _tone("a1t1", 2.0))], total_s=4.0)
    full = Capture.load(tmp_path, "p", MANIFEST)
    since = full.start + 0.5
    capture = Capture.load(tmp_path, "p", MANIFEST, since=since)
    first = capture.first_sound(since)
    assert first is not None
    assert first - full.start == pytest.approx(1.0, abs=0.01)
    assert capture.first_sound(full.start + 3.5) is None


def test_hear_load_rejects_tone_playing_before_command(tmp_path: Path) -> None:
    """A tone already playing at the command is rejected, one starting in its window is not."""
    _write(tmp_path, "p", [(0.5, _tone("a1t1", 3.0))], total_s=8.0)
    full = Capture.load(tmp_path, "p", MANIFEST)
    for offset, accepted in ((1.0, False), (0.47, True)):
        after = full.start + offset
        capture = Capture.load(tmp_path, "p", MANIFEST, since=after - HEAR_LEAD_S)
        first = next(s for s in capture.timeline() if s.kind == "a1t1")
        assert starts_after(first, after, WINDOW_S) is accepted


def test_crossfade_is_overlap(tmp_path: Path) -> None:
    """A 3 s linear crossfade shows as one overlap from the outgoing to the incoming tone."""
    rate, fade = 48000, 3 * 48000
    out, inc = _tone("a1t1", 6.0), _tone("a1t2", 6.0)
    out[-fade:] *= np.linspace(1.0, 0.0, fade)
    inc[:fade] *= np.linspace(0.0, 1.0, fade)
    mix = np.zeros(9 * rate)
    mix[: 6 * rate] += out
    mix[3 * rate :] += inc
    _write(tmp_path, "p", [(0.0, mix)], total_s=9.0)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    (overlap,) = capture.overlaps()
    assert (overlap.outgoing, overlap.incoming) == ("a1t1", "a1t2")
    assert overlap.duration == pytest.approx(3.0, abs=0.4)


@pytest.mark.parametrize("track_id", [t.id for t in MANIFEST.tracks])
def test_resampled_input_identified(tmp_path: Path, track_id: str) -> None:
    """Every tone is still identified after resampling to 44.1 kHz."""
    resampled = resample_poly(_tone(track_id, 3.0), 147, 160)
    _write(tmp_path, "p", [(0.5, resampled)], total_s=4.0, rate=44100)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    assert [s.kind for s in capture.timeline() if s.kind != "silence"] == [track_id]


def test_marker_alignment(tmp_path: Path) -> None:
    """Chirp markers land within 5 ms and give the sync offset between two players."""
    signal = _tone("a1t1", 4.0)
    _write(tmp_path, "a", [(1.0, signal)], total_s=6.0)
    _write(tmp_path, "b", [(1.037, signal)], total_s=6.0)
    a = Capture.load(tmp_path, "a", MANIFEST)
    b = Capture.load(tmp_path, "b", MANIFEST)
    expected = [a.start + 1.0, a.start + 1.0 + 4.0 - MANIFEST.chirp.duration_s]
    assert a.markers() == pytest.approx(expected, abs=0.005)
    assert a.sync_offset(b) == pytest.approx(0.037, abs=0.005)


@pytest.mark.parametrize(("lag", "expected"), [(0.003, 0.003), (0.15, 0.15)])
def test_sync_offset_without_first_marker(tmp_path: Path, lag: float, expected: float) -> None:
    """A lost start marker leaves the offset to the boundary markers, which pair unambiguously."""
    # cut before track 2's end marker, as the in_sync check reads right after the advance
    gapless = np.concatenate([_tone("a1t1", 4.0), _tone("a1t2", 4.0)])[: 7 * 48000]
    garbled = gapless.copy()
    garbled[: round(MANIFEST.chirp.duration_s * 48000)] = 0.0
    _write(tmp_path, "a", [(1.0, gapless)], total_s=10.0)
    _write(tmp_path, "b", [(1.0 + lag, garbled)], total_s=10.0)
    a = Capture.load(tmp_path, "a", MANIFEST)
    b = Capture.load(tmp_path, "b", MANIFEST)
    assert a.sync_offset(b) == pytest.approx(expected, abs=0.005)


@pytest.mark.parametrize("dropped", [1, 48])
def test_boundary_markers_survive_dropped_frames(tmp_path: Path, dropped: int) -> None:
    """A player that drops frames at a track change to stay in sync keeps both boundary markers."""
    gapless = np.concatenate([_tone("a1t1", 4.0), _tone("a1t2", 4.0)])[: 7 * 48000]
    # at the very end of track 1: its end marker stays where it is, track 2's start marker
    # comes that many frames sooner
    cut = 4 * 48000 - 240
    corrected = np.delete(gapless, slice(cut, cut + dropped))
    _write(tmp_path, "a", [(1.0, gapless)], total_s=10.0)
    _write(tmp_path, "b", [(1.1, corrected)], total_s=10.0)
    a = Capture.load(tmp_path, "a", MANIFEST)
    b = Capture.load(tmp_path, "b", MANIFEST)
    first, second = b.markers()[1:]
    assert round((second - first) * 48000) == 9600 - dropped
    # past the start marker, as the in_sync checks read it
    since = a.start + 3.0
    assert Capture.load(tmp_path, "a", MANIFEST, since=since).sync_offset(
        Capture.load(tmp_path, "b", MANIFEST, since=since)
    ) == pytest.approx(0.1, abs=0.005)


def test_markers_survive_stamp_jitter(tmp_path: Path) -> None:
    """Sub-millisecond jitter on write stamps does not cut gaps into a chirp."""
    _write(tmp_path, "p", [(1.0, _tone("a1t1", 4.0))], total_s=6.0)
    stamps = tmp_path / "p.stamps"
    lines = [line.split() for line in stamps.read_text().splitlines()]
    jitter = np.random.default_rng(6839).uniform(0, 400_000, len(lines)).round().astype(int)
    stamps.write_text(
        "".join(
            f"{m} {int(wall) + j} {off} {n}\n"
            for (m, wall, off, n), j in zip(lines, jitter, strict=True)
        )
    )
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.0, capture.start + 1.0 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)


def test_one_late_write_is_not_a_gap(tmp_path: Path) -> None:
    """A write read 6 ms late, with the next ones on cadence, does not cut a hole into a chirp."""
    _write(tmp_path, "p", [(1.0, _tone("a1t1", 4.0))], total_s=6.0)
    # write 2 is 0.1 s into the start chirp
    _split_into_reads(tmp_path, "p", lambda write: 6_000_000 if write == 2 else 0)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.0, capture.start + 1.0 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)


def test_cadence_slip_is_a_gap(tmp_path: Path) -> None:
    """A writer that falls 20 ms behind and stays there moves the rest of the audio 20 ms."""
    _write(tmp_path, "p", [(1.0, _tone("a1t1", 4.0))], total_s=6.0)
    # the first slipped write is read another 10 ms late on top
    _split_into_reads(
        tmp_path, "p", lambda write: 30_000_000 if write == 40 else 20_000_000 * (write > 40)
    )
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.0, capture.start + 1.02 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)


def test_stall_made_up_by_a_burst_is_a_gap(tmp_path: Path) -> None:
    """A writer stalling 300 ms that then writes its backlog at once leaves a 300 ms gap."""
    _write(tmp_path, "p", [(1.0, _tone("a1t1", 4.0))], total_s=6.0)
    # writes 40 to 45 come out together at write 46's time, the ones after on cadence
    _split_into_reads(
        tmp_path, "p", lambda write: (46 - write) * 50_000_000 if 40 <= write < 46 else 0
    )
    capture = Capture.load(tmp_path, "p", MANIFEST)
    (gap,) = capture.gaps()
    assert gap.duration == pytest.approx(0.3, abs=2 * WINDOW_S)


def test_wake_up_the_pacer_makes_up_for_is_not_a_gap(tmp_path: Path) -> None:
    """A pacer that wakes 120 ms late and reads its backlog at once leaves the audio in place."""
    # read 10 is 0.2 s in, well before the tone
    _pace(tmp_path, "p", _idle_then_tone(), {10: 120_000_000})
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.0, capture.start + 1.0 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)


def test_pacer_late_for_a_while_is_not_a_gap(tmp_path: Path) -> None:
    """A pacer that wakes 10 ms late for a third of a second leaves the audio in place."""
    _pace(tmp_path, "p", _idle_then_tone(), dict.fromkeys(range(10, 26), 10_000_000))
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.0, capture.start + 1.0 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)


async def test_sendspin_capture_stamps_play_at_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 250 ms late wake-up and chunks sent after play-at leave the audio in place."""
    # 50 ms chunks sent 200 ms ahead, until the server falls 30 ms behind from 3.5 s on
    late = await _capture_sendspin(
        tmp_path,
        monkeypatch,
        50_000_000,
        lambda i, play_ns: play_ns + (30_000_000 if i >= 60 else -200_000_000),
        late_wake_ns={10: 250_000_000},
    )
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.5, capture.start + 1.5 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)
    assert late == [0] * 60 + [30_000_000] * (len(late) - 60)


async def test_sendspin_capture_keeps_a_late_burst_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks that arrive at once, up to 142 ms after their play-at time, leave the audio in place.

    The arrivals of run 20261118-141911-a467 under CPU steal: the first 25 ms chunk came 10 ms
    late, and later six chunks came together, 142 to 17 ms after their play-at time.
    """
    burst = range(120, 126)

    def sent_ns(i: int, play_ns: int) -> int:
        if i == 0:
            return play_ns + 10_400_000
        if i in burst:
            return play_ns + (burst[-1] - i) * 25_000_000 + 17_400_000
        return play_ns - 200_000_000

    late = await _capture_sendspin(tmp_path, monkeypatch, 25_000_000, sent_ns)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    expected = [capture.start + 1.5, capture.start + 1.5 + 4.0 - MANIFEST.chirp.duration_s]
    assert capture.markers() == pytest.approx(expected, abs=0.005)
    expected_late = [0] * len(late)
    expected_late[0] = 10_400_000
    for i in burst:
        expected_late[i] = (burst[-1] - i) * 25_000_000 + 17_400_000
    assert late == expected_late


def test_level_follows_volume(tmp_path: Path) -> None:
    """The level of a span drops 12 dB with the signal and counts only the asked kind."""
    loud = _tone("a1t4", 4.0)
    quiet = loud * 10 ** (-12 / 20)
    _write(tmp_path, "p", [(0.0, loud[: 2 * 48000]), (2.0, quiet[2 * 48000 :])], total_s=5.0)
    capture = Capture.load(tmp_path, "p", MANIFEST)
    before = capture.level(capture.start + 0.5, capture.start + 1.5, "a1t4")
    after = capture.level(capture.start + 2.5, capture.start + 3.5, "a1t4")
    assert before is not None and after is not None
    assert before - after == pytest.approx(12.0, abs=0.5)
    assert capture.level(capture.start + 4.2, capture.start + 5.0, "a1t4") is None


def _tone(track_id: str, seconds: float) -> npt.NDArray[np.float64]:
    """Synthesize a shortened manifest track at 48 kHz."""
    track = next(t for t in MANIFEST.tracks if t.id == track_id)
    return synthesize(replace(track, duration_s=seconds), MANIFEST.chirp)


def _write(
    directory: Path,
    role: str,
    pieces: list[tuple[float, npt.NDArray[np.float64]]],
    total_s: float,
    rate: int = 48000,
) -> None:
    """Write the pacer's three capture files, stamping 50 ms writes at each piece's offset."""
    chunk = rate // 20
    pcm = bytearray()
    stamps = []
    for start_s, signal in pieces:
        frames = np.repeat(np.round(signal * 32767).astype("<i2"), 2).tobytes()
        for i in range(0, len(frames), chunk * 4):
            data = frames[i : i + chunk * 4]
            wall_ns = BASE_NS + round((start_s + i / 4 / rate) * 1e9)
            stamps.append(f"{wall_ns - BASE_NS} {wall_ns} {len(pcm)} {len(data)}\n")
            pcm += data
    (directory / f"{role}.pcm").write_bytes(bytes(pcm))
    (directory / f"{role}.stamps").write_text("".join(stamps))
    meta = {
        "role": role,
        "sample_rate": rate,
        "channels": 2,
        "bits": 16,
        "encoding": "s16le",
        "started_wall_ns": BASE_NS,
        "started_monotonic_ns": 0,
        "container_started_wall_ns": BASE_NS,
        "bytes_total": len(pcm),
        "ended_wall_ns": BASE_NS + round(total_s * 1e9),
    }
    (directory / f"{role}.json").write_text(json.dumps(meta))


def _idle_then_tone() -> npt.NDArray[np.float64]:
    """Return what an idle player writes around a 4 s tone: 1 s of silence, the tone, silence."""
    return np.concatenate([np.zeros(48000), _tone("a1t1", 4.0), np.zeros(24000)])


def _pace(
    directory: Path, role: str, signal: npt.NDArray[np.float64], late_ns: dict[int, int]
) -> None:
    """Write a capture through the pacer, which oversleeps by `late_ns[read]` before a read."""
    now = [10**9]
    read = [0]
    pcm = np.repeat(np.round(signal * 32767).astype("<i2"), 2).tobytes()

    def clock() -> int:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += round(seconds * 1e9) + late_ns.get(read[0], 0)

    def reads() -> Iterator[bytes]:
        for start in range(0, len(pcm), READ_BYTES):
            read[0] = start // READ_BYTES
            yield pcm[start : start + READ_BYTES]

    writer = CaptureWriter(
        role,
        directory,
        48000,
        2,
        container_started_wall_ns=BASE_NS,
        monotonic_ns=clock,
        wall_ns=lambda: BASE_NS + now[0],
    )
    pump(reads(), writer, RateLimiter(192000, monotonic_ns=clock, sleep=sleep))
    writer.close()


def _split_into_reads(directory: Path, role: str, delay_ns: Callable[[int], int]) -> None:
    """Restamp each write as the pacer's 4096-byte reads, 50 µs apart, delayed per write."""
    stamps = directory / f"{role}.stamps"
    lines = []
    for write, line in enumerate(stamps.read_text().splitlines()):
        mono, wall, offset, nbytes = (int(field) for field in line.split())
        for k, start in enumerate(range(0, nbytes, 4096)):
            shift = delay_ns(write) + k * 50_000
            size = min(4096, nbytes - start)
            lines.append(f"{mono + shift} {wall + shift} {offset + start} {size}\n")
    stamps.write_text("".join(lines))


async def _capture_sendspin(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunk_ns: int,
    sent_ns: Callable[[int, int], int],
    late_wake_ns: dict[int, int] | None = None,
) -> list[int]:
    """Capture `_idle_then_tone()` through the Sendspin client on a virtual clock.

    Chunk i plays at 0.5 s + i * chunk_ns and arrives at sent_ns(i, play_ns), or right after the
    chunk before it when that is later; the client oversleeps by `late_wake_ns[sleep call]`.
    Return the lateness each stamp line records.
    """
    sendspin_client = pytest.importorskip("labctl.sendspin_client")
    clock = _VirtualClock(late_ns=late_wake_ns or {})
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    client = _FakeSendspinClient(clock)
    writer = CaptureWriter(
        "p",
        directory,
        48000,
        2,
        container_started_wall_ns=BASE_NS,
        monotonic_ns=lambda: clock.now,
        wall_ns=lambda: BASE_NS + clock.now,
    )
    capture_task = asyncio.create_task(
        sendspin_client.SendspinCapture(client, writer, wall_ns=lambda: BASE_NS + clock.now).run()
    )
    pcm = np.repeat(np.round(_idle_then_tone() * 32767).astype("<i2"), 2).tobytes()
    chunk_bytes = chunk_ns * 48000 // 10**9 * 4
    for i, start in enumerate(range(0, len(pcm), chunk_bytes)):
        play_ns = 500_000_000 + i * chunk_ns
        await clock.advance_to(sent_ns(i, play_ns))
        client.deliver(play_ns // 1000, pcm[start : start + chunk_bytes])
        await clock.settle()
    await clock.advance_to(10**10)
    capture_task.cancel()
    with suppress(asyncio.CancelledError):
        await capture_task
    writer.close()
    stamps = (directory / "p.stamps").read_text().splitlines()
    return [int(line.split()[4]) for line in stamps]


class _VirtualClock:
    """Monotonic ns clock that `asyncio.sleep` advances, oversleeping by `late_ns[call]`."""

    def __init__(self, late_ns: dict[int, int]) -> None:
        self.now = 0
        self._late_ns = late_ns
        self._calls = count()
        self._sleepers: list[tuple[int, int, asyncio.Future[None]]] = []
        self._yield = asyncio.sleep

    async def sleep(self, seconds: float) -> None:
        call = next(self._calls)
        wake = self.now + round(seconds * 1e9) + self._late_ns.get(call, 0)
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (wake, call, future))
        await future

    async def advance_to(self, ns: int) -> None:
        """Wake every sleeper due by ns in order, then set the clock to ns."""
        while self._sleepers and self._sleepers[0][0] <= ns:
            wake, _, future = heapq.heappop(self._sleepers)
            self.now = wake
            future.set_result(None)
            await self.settle()
        self.now = max(self.now, ns)

    async def settle(self) -> None:
        """Let woken tasks run until they block again."""
        for _ in range(3):
            await self._yield(0)


class _FakeSendspinClient:
    """Sendspin client whose server clock is the virtual clock in µs."""

    def __init__(self, clock: _VirtualClock) -> None:
        self._clock = clock
        self._on_chunk: Callable[..., None] | None = None

    def add_audio_chunk_listener(self, callback: Callable[..., None]) -> None:
        self._on_chunk = callback

    def add_stream_clear_listener(self, callback: Callable[..., None]) -> None:
        pass

    def add_stream_end_listener(self, callback: Callable[..., None]) -> None:
        pass

    def compute_play_time(self, server_timestamp_us: int) -> int:
        return server_timestamp_us

    def now_us(self) -> int:
        return self._clock.now // 1000

    def deliver(self, play_us: int, payload: bytes) -> None:
        assert self._on_chunk is not None
        self._on_chunk(play_us, payload, None, 0)
