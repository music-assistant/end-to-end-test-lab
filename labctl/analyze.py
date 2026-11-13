"""Audio oracle: turn a pacer capture into a wall-clock timeline of tones, silence and markers."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import numpy.typing as npt
from scipy import signal as sps

WINDOW = 4096
SILENCE_DB = -60.0
# manifest tones sit at least 70 Hz apart; boundary windows and clock skew shift a few Hz
TONE_TOLERANCE_HZ = 5.0
# a window is silent when most of its RMS blocks (about 10 ms each at 4096) are below SILENCE_DB
SILENCE_BLOCKS = 8
# a second identified tone within this distance of the dominant one makes the window "mixed"
MIX_DB = 30.0
MARKER_THRESHOLD = 0.5
MARKER_PAIR_S = 0.5
# markers land within 5 ms of the chirp, so pairs of one offset agree to well within this
MARKER_AGREE_S = 0.02
# write stamps wobble by up to a few ms; a gap that small is jitter, not missing audio
STAMP_JITTER_S = 0.005
# a read up to LATE_READ late opens only the gap the writes SETTLE to CONFIRM after it confirm;
# a longer lag is a stall, even when a catch-up burst makes up for it
STAMP_LATE_READ_S = 0.05
STAMP_SETTLE_S = 0.04
STAMP_CONFIRM_S = 0.25

Samples = npt.NDArray[np.float32]


@dataclass(frozen=True)
class Chirp:
    """Linear chirp marker placed at the start and end of every tone track."""

    duration_s: float
    f0_hz: float
    f1_hz: float


@dataclass(frozen=True)
class Track:
    """One tone track of the generated library."""

    id: str
    artist: str
    album: str
    title: str
    track: int
    date: str
    path: str
    freq_hz: float
    duration_s: float


@dataclass(frozen=True)
class Manifest:
    """The tone library description shared by the generator and the oracle."""

    version: int
    chirp: Chirp
    tracks: tuple[Track, ...]

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Read a manifest.json."""
        raw = json.loads(path.read_text())
        return cls(
            version=int(raw["version"]),
            chirp=Chirp(**raw["chirp"]),
            tracks=tuple(Track(**track) for track in raw["tracks"]),
        )


class Segment(NamedTuple):
    """A run of windows with the same classification; times are wall-clock seconds."""

    kind: str
    t0: float
    t1: float
    level: float

    @property
    def duration(self) -> float:
        """Length of the segment in seconds."""
        return self.t1 - self.t0


class Overlap(NamedTuple):
    """A span where one tone fades out while the next fades in."""

    t0: float
    t1: float
    outgoing: str
    incoming: str

    @property
    def duration(self) -> float:
        """Length of the overlap in seconds."""
        return self.t1 - self.t0


def chirp_template(chirp: Chirp, rate: int) -> npt.NDArray[np.float64]:
    """Return the unit-amplitude marker chirp at the given sample rate."""
    t = np.arange(round(chirp.duration_s * rate)) / rate
    result: npt.NDArray[np.float64] = sps.chirp(
        t, chirp.f0_hz, chirp.duration_s, chirp.f1_hz, method="linear"
    )
    return result


@dataclass
class Capture:
    """Mono audio of one player on a wall-clock time axis, with unwritten time as silence."""

    role: str
    rate: int
    start: float
    samples: Samples
    manifest: Manifest
    # wall-clock time the capture sidecar finalized its files, None while it still writes
    ended: float | None = None

    @classmethod
    def load(
        cls, captures_dir: Path, role: str, manifest: Manifest, since: float | None = None
    ) -> Capture:
        """Load `<role>.pcm/.stamps/.json`, reading only what the pacer has written so far.

        With `since` (wall-clock seconds) only the audio from about then on is loaded, which
        keeps a read cheap late in a long run; windows classify exactly as in a full load.
        """
        meta = json.loads((captures_dir / f"{role}.json").read_text())
        rate = int(meta["sample_rate"])
        channels = int(meta["channels"])
        frame_bytes = 2 * channels
        pcm_path = captures_dir / f"{role}.pcm"
        frames_total = pcm_path.stat().st_size // frame_bytes

        origin_ns = int(meta["started_wall_ns"])
        reads: list[tuple[int, int, int]] = []
        for wall_ns, offset, nbytes in _read_stamps(captures_dir / f"{role}.stamps"):
            f0 = offset // frame_bytes
            f1 = min((offset + nbytes) // frame_bytes, frames_total)
            if f1 > f0:
                reads.append((round((wall_ns - origin_ns) * rate / 1e9), f0, f1))
        placements = _place_reads(reads, rate)
        end = placements[-1][0] + placements[-1][2] - placements[-1][1] if placements else 0

        # whole windows only, so the trimmed capture keeps the full load's window grid
        first = 0
        if since is not None:
            first = max(math.floor((since - origin_ns / 1e9) * rate / WINDOW) * WINDOW, 0)
        placements = [(pos, f0, f1) for pos, f0, f1 in placements if pos + f1 - f0 > first]
        read_from = placements[0][1] if placements else frames_total
        with pcm_path.open("rb") as fh:
            fh.seek(read_from * frame_bytes)
            raw = fh.read((frames_total - read_from) * frame_bytes)
        frames = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
        mono = (frames.mean(axis=1) / 32768.0).astype(np.float32)

        ended_ns = meta.get("ended_wall_ns")
        stop_ns = time.time_ns() if ended_ns is None else int(ended_ns)
        length = max(end, round((stop_ns - origin_ns) * rate / 1e9)) - first
        samples = np.zeros(max(length, 0), dtype=np.float32)
        for pos, f0, f1 in placements:
            skip = max(first - pos, 0)
            samples[pos + skip - first : pos + f1 - f0 - first] = mono[
                f0 + skip - read_from : f1 - read_from
            ]
        return cls(
            role=role,
            rate=rate,
            start=origin_ns / 1e9 + first / rate,
            samples=samples,
            manifest=manifest,
            ended=None if ended_ns is None else int(ended_ns) / 1e9,
        )

    @property
    def end(self) -> float:
        """Wall-clock time of the end of the capture."""
        return self.start + len(self.samples) / self.rate

    def timeline(self, window: int = WINDOW) -> list[Segment]:
        """Classify consecutive windows as a tone id, `silence`, `mixed` or `unknown`."""
        windows = self._windows(window)
        return [
            Segment(
                kind,
                self._time(i0 * window),
                self._time(i1 * window),
                float(np.median([w.level for w in windows[i0:i1]])),
            )
            for kind, i0, i1 in _runs([w.kind for w in windows])
        ]

    def gaps(self) -> list[Segment]:
        """Return silence segments that have sound on both sides."""
        return [segment for segment in self.timeline()[1:-1] if segment.kind == "silence"]

    def overlaps(self) -> list[Overlap]:
        """Return crossfades: spans of two tones where one ramps down and the other up."""
        windows = self._windows(WINDOW)
        keys = [(w.kind, tuple(sorted(w.tones))) for w in windows]
        result: list[Overlap] = []
        for (kind, ids), i0, i1 in _runs(keys):
            if kind != "mixed":
                continue
            first, last = windows[i0].tones, windows[i1 - 1].tones
            outgoing = min(ids, key=lambda tone: last[tone] - first[tone])
            incoming = ids[1] if outgoing == ids[0] else ids[0]
            result.append(
                Overlap(self._time(i0 * WINDOW), self._time(i1 * WINDOW), outgoing, incoming)
            )
        return result

    def markers(self) -> list[float]:
        """Return wall-clock start times of the chirp markers."""
        template = chirp_template(self.manifest.chirp, self.rate)
        if len(self.samples) < len(template):
            return []
        x = self.samples.astype(np.float64)
        corr = sps.correlate(x, template, mode="valid", method="fft")
        energy = np.concatenate(([0.0], np.cumsum(x * x)))
        local = np.maximum(energy[len(template) :] - energy[: -len(template)], 0.0)
        # normalised cross-correlation: 1.0 is a lone chirp, independent of playback volume
        ncc = corr / (np.linalg.norm(template) * np.sqrt(local) + 1e-12)
        # padded so a chirp at the very start or end of the capture still counts as a peak
        peaks, _ = sps.find_peaks(np.pad(ncc, 1), height=MARKER_THRESHOLD, distance=len(template))
        return [self._time(int(p) - 1) for p in peaks]

    def sync_offset(self, other: Capture) -> float | None:
        """Return how many seconds `other` lags this capture, or None without one agreed offset.

        The offset is the one most marker pairs agree on, so a lost marker cannot pair with a
        neighbour 0.2 s away (track end and next start) and pass off a large lag as a small one.
        """
        theirs = other.markers()
        deltas = [b - a for a in self.markers() for b in theirs if abs(b - a) <= MARKER_PAIR_S]
        groups = [[d for d in deltas if abs(d - delta) <= MARKER_AGREE_S] for delta in deltas]
        best = max(map(len, groups), default=0)
        medians = [float(np.median(g)) for g in groups if len(g) == best]
        if not medians or max(medians) - min(medians) > MARKER_AGREE_S:
            return None
        return medians[0]

    def level(self, t0: float, t1: float, kind: str | None = None) -> float | None:
        """Return the median level in dBFS of the windows inside [t0, t1], only `kind` if given.

        None when no such window was captured.
        """
        first = max(math.ceil((t0 - self.start) * self.rate / WINDOW), 0)
        last = math.floor((t1 - self.start) * self.rate / WINDOW)
        levels = [w.level for w in self._windows(WINDOW, first, last) if kind in (None, w.kind)]
        return float(np.median(levels)) if levels else None

    def _time(self, sample: int) -> float:
        """Wall-clock time of a sample index."""
        return self.start + sample / self.rate

    def _windows(self, window: int, first: int = 0, last: int | None = None) -> list[_Window]:
        """Classify the non-overlapping windows of the capture, from index `first` to `last`."""
        hann = sps.get_window("hann", window)
        gain = 2.0 / hann.sum()
        freqs = np.array([track.freq_hz for track in self.manifest.tracks])
        ids = [track.id for track in self.manifest.tracks]
        result: list[_Window] = []
        count = len(self.samples) // window
        for i in range(first, count if last is None else min(last, count)):
            chunk = self.samples[i * window : (i + 1) * window].astype(np.float64)
            level = _rms_db(chunk)
            blocks = chunk.reshape(SILENCE_BLOCKS, -1)
            silent = sum(_rms_db(block) < SILENCE_DB for block in blocks)
            if silent * 2 >= SILENCE_BLOCKS:
                result.append(_Window("silence", level, {}))
                continue
            spectrum = np.abs(np.fft.rfft(chunk * hann)) * gain
            k1 = int(np.argmax(spectrum[1:-1])) + 1
            f1, a1 = _refine(spectrum, k1, self.rate, window)
            tone1 = _match(f1, freqs, ids)
            if tone1 is None:
                result.append(_Window("unknown", level, {}))
                continue
            rest = spectrum.copy()
            rest[max(k1 - 3, 0) : k1 + 4] = 0.0
            k2 = int(np.argmax(rest[1:-1])) + 1
            f2, a2 = _refine(spectrum, k2, self.rate, window)
            tone2 = _match(f2, freqs, ids)
            if tone2 is not None and tone2 != tone1 and a2 >= a1 - MIX_DB:
                result.append(_Window("mixed", level, {tone1: a1, tone2: a2}))
            else:
                result.append(_Window(tone1, level, {tone1: a1}))
        return result


class _Window(NamedTuple):
    """Classification of one analysis window; tones maps tone id to peak level in dBFS."""

    kind: str
    level: float
    tones: dict[str, float]


def _read_stamps(path: Path) -> list[tuple[int, int, int]]:
    """Parse complete stamp lines into (wall_ns, byte_offset, nbytes)."""
    # the last element is "" or a line the pacer is still writing
    lines = path.read_text().split("\n")[:-1]
    return [(int(f[1]), int(f[2]), int(f[3])) for f in (line.split() for line in lines)]


def _place_reads(reads: list[tuple[int, int, int]], rate: int) -> list[tuple[int, int, int]]:
    """Place (stamped frame, f0, f1) reads on the timeline as (position, f0, f1)."""
    jitter = round(STAMP_JITTER_S * rate)
    late_read = round(STAMP_LATE_READ_S * rate)
    settle = round(STAMP_SETTLE_S * rate)
    confirm = round(STAMP_CONFIRM_S * rate)
    placements: list[tuple[int, int, int]] = []
    end = 0
    for i, (stamped, f0, f1) in enumerate(reads):
        # a pipe can hand over a backlog in one burst: bytes never play before earlier bytes end
        lag = stamped - end
        if jitter < lag <= late_read:
            # a writer that only fired late is back on its old schedule a write later; only the
            # first read of a write tells, the reads after it always look early
            ahead = end + f1 - f0
            j = i + 1
            while j < len(reads) and reads[j][0] - stamped <= confirm:
                first_read = reads[j][0] - reads[j - 1][0] > jitter
                if first_read and reads[j][0] - stamped >= settle:
                    lag = min(lag, reads[j][0] - ahead)
                ahead += reads[j][2] - reads[j][1]
                j += 1
        pos = end + lag if lag > jitter else end
        placements.append((pos, f0, f1))
        end = pos + f1 - f0
    return placements


def _refine(
    spectrum: npt.NDArray[np.float64], k: int, rate: int, window: int
) -> tuple[float, float]:
    """Parabolic interpolation on log magnitude: (frequency Hz, peak amplitude dBFS)."""
    alpha, beta, gamma = np.log(np.maximum(spectrum[k - 1 : k + 2], 1e-12))
    denom = alpha - 2 * beta + gamma
    p = 0.5 * (alpha - gamma) / denom if denom else 0.0
    peak = beta - 0.25 * (alpha - gamma) * p
    return (k + p) * rate / window, _db(float(np.exp(peak)))


def _match(freq: float, freqs: npt.NDArray[np.float64], ids: list[str]) -> str | None:
    """Return the tone id nearest to freq within tolerance."""
    nearest = int(np.argmin(np.abs(freqs - freq)))
    return ids[nearest] if abs(freqs[nearest] - freq) <= TONE_TOLERANCE_HZ else None


def _runs[T](keys: list[T]) -> list[tuple[T, int, int]]:
    """Group consecutive equal keys into (key, start index, end index)."""
    result: list[tuple[T, int, int]] = []
    for i, key in enumerate(keys):
        if result and result[-1][0] == key:
            result[-1] = (key, result[-1][1], i + 1)
        else:
            result.append((key, i, i + 1))
    return result


def _rms_db(x: npt.NDArray[np.float64]) -> float:
    """RMS level in dBFS, referenced so a full-scale sine reads 0 dB."""
    return _db(float(np.sqrt(np.mean(x * x) * 2.0)))


def _db(amplitude: float) -> float:
    """Amplitude to dBFS with a floor for digital silence."""
    return float(20.0 * np.log10(max(amplitude, 1e-10)))
