"""Generate the FLAC tone library described in manifest.json."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

import numpy as np
import numpy.typing as npt

from labctl.analyze import Chirp, Manifest, Track, chirp_template

MANIFEST = Path(__file__).with_name("manifest.json")
RATE = 48000
AMPLITUDE = 10 ** (-12 / 20)
FADE_S = 0.005


def main() -> None:
    """Command line entry point: `python -m tones.generate --out <dir>`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="library root directory")
    out = parser.parse_args().out
    if generate(out):
        print(f"generated {len(Manifest.load(MANIFEST).tracks)} tracks in {out}")
    else:
        print(f"{out} is up to date")


def generate(out: Path) -> bool:
    """Write every manifest track under out; return False when the version already matches."""
    version = _version()
    version_file = out / ".version"
    if version_file.is_file() and version_file.read_text().strip() == version:
        return False
    manifest = Manifest.load(MANIFEST)
    out.mkdir(parents=True, exist_ok=True)
    # drop tracks of an older manifest so the library holds exactly the manifest
    for old in out.rglob("*.flac"):
        old.unlink()
    for track in manifest.tracks:
        dest = out / track.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        encode(synthesize(track, manifest.chirp), track, dest)
    version_file.write_text(version + "\n")
    return True


def synthesize(track: Track, chirp: Chirp, rate: int = RATE) -> npt.NDArray[np.float64]:
    """Return the mono track signal: the tone with a chirp marker over its first and last 200 ms."""
    t = np.arange(round(track.duration_s * rate)) / rate
    signal = _fade(np.sin(2 * np.pi * track.freq_hz * t), rate)
    marker = _fade(chirp_template(chirp, rate), rate)
    signal[: len(marker)] += marker
    signal[-len(marker) :] += marker
    result: npt.NDArray[np.float64] = signal * AMPLITUDE
    return result


def encode(signal: npt.NDArray[np.float64], track: Track, dest: Path) -> None:
    """Encode a mono signal as tagged stereo FLAC through the ffmpeg CLI."""
    pcm = np.repeat(np.round(signal * 32767).astype("<i2"), 2).tobytes()
    tags = {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "album_artist": track.artist,
        "track": str(track.track),
        "date": track.date,
    }
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-c:a", "flac", str(dest)]
    subprocess.run(cmd, input=pcm, check=True)


def _version() -> str:
    """Hash of the generator and the manifest."""
    digest = hashlib.sha256(Path(__file__).read_bytes() + MANIFEST.read_bytes())
    return digest.hexdigest()[:16]


def _fade(x: npt.NDArray[np.float64], rate: int) -> npt.NDArray[np.float64]:
    """Apply raised-cosine fade in and out."""
    n = round(FADE_S * rate)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return x


if __name__ == "__main__":
    main()
