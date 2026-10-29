"""Tone manifest: every tone stays identifiable next to every other."""

import itertools
from pathlib import Path

from labctl.analyze import WINDOW, Manifest
from tones.generate import RATE

MANIFEST = Path(__file__).parents[1] / "tones" / "manifest.json"
MIN_SPACING_HZ = 70.0


def test_tones_are_spaced_and_bin_aligned() -> None:
    """Tone frequencies are at least 70 Hz apart and sit on an analysis window bin."""
    freqs = [track.freq_hz for track in Manifest.load(MANIFEST).tracks]
    close = [(a, b) for a, b in itertools.combinations(freqs, 2) if abs(a - b) < MIN_SPACING_HZ]
    assert not close, f"tones closer than {MIN_SPACING_HZ} Hz: {close}"
    off_bin = [freq for freq in freqs if (freq * WINDOW / RATE) % 1]
    assert not off_bin, f"tones off a {RATE / WINDOW} Hz bin: {off_bin}"
