"""Audio oracle tests on synthetic captures."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from scipy.signal import resample_poly

from labctl.analyze import Capture, Manifest
from labctl.config import REPO_ROOT
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
