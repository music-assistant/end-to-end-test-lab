"""Mock internet radio for lab runs: ICY and live HLS streams, announce clips, fault injection."""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from aiohttp import web

from labctl.analyze import Manifest
from tones.generate import RATE, synthesize

BITRATE = 128_000
BYTES_PER_SECOND = BITRATE // 8
# one MPEG-1 layer III frame at 128 kb/s and 48 kHz, so loop offsets stay frame aligned
FRAME_BYTES = 384
CHUNK_BYTES = FRAME_BYTES * 10
# Icecast's burst-on-connect: a new listener gets the last 64 KB at once, then real-time pace
BURST_BYTES = 65536 // FRAME_BYTES * FRAME_BYTES
ICY_METAINT = 16000
TITLE_PERIOD_S = 10
HLS_PLAYLIST = "live.m3u8"
# segments the playlist must list before /health reports ready
HLS_MIN_SEGMENTS = 3
HLS_CONTENT_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}
HLS_NAME = re.compile(r"^[\w-]+\.(m3u8|ts)$")
# manifest tones served whole as MP3 files at /tone/<id>.mp3, for announcements
CLIP_TONES = ("a3t1", "a4t1")


@dataclass
class RadioState:
    """What the control endpoints read and change."""

    mp3: bytes
    hls_dir: Path | None
    clips: dict[str, bytes] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    flaky_armed: bool = True
    hls_process: asyncio.subprocess.Process | None = None
    streams: set[asyncio.Event] = field(default_factory=set)

    @property
    def elapsed(self) -> float:
        """Seconds since the service started."""
        return time.monotonic() - self.started

    def take_flaky(self) -> bool:
        """Return whether /flaky must refuse this request, disarming it if so."""
        armed, self.flaky_armed = self.flaky_armed, False
        return armed

    def drop(self) -> int:
        """End every running audio stream and return how many there were."""
        for stop in self.streams:
            stop.set()
        return len(self.streams)

    def hls_ready(self) -> bool:
        """Return whether the HLS encoder runs and its playlist lists enough segments."""
        if self.hls_dir is None:
            return True
        if self.hls_process is None or self.hls_process.returncode is not None:
            return False
        playlist = self.hls_dir / HLS_PLAYLIST
        return playlist.is_file() and playlist.read_text().count("#EXTINF") >= HLS_MIN_SEGMENTS


STATE = web.AppKey("state", RadioState)


def main() -> None:
    """Command line entry point: `lab-radio --manifest <path> --work <dir>`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="tone manifest.json")
    parser.add_argument("--tone", default="a2t4", help="manifest track id to stream")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--work", type=Path, required=True, help="dir for generated audio")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    mp3 = _encode_mp3(args.manifest, args.tone, args.work / "tone.mp3")
    hls_dir = args.work / "hls"
    hls_dir.mkdir(exist_ok=True)
    clips = {
        tone: _encode_mp3(args.manifest, tone, args.work / f"{tone}.mp3") for tone in CLIP_TONES
    }
    web.run_app(build_app(mp3, hls_dir, clips), port=args.port, print=None)


def build_app(
    mp3: bytes, hls_dir: Path | None, clips: dict[str, bytes] | None = None
) -> web.Application:
    """Return the radio app streaming `mp3` and serving `clips` by tone id.

    With `hls_dir`, it also encodes and serves live HLS.
    """
    app = web.Application()
    app[STATE] = RadioState(mp3, hls_dir, clips or {})
    if hls_dir is not None:
        app.cleanup_ctx.append(_hls_encoder)
        app.router.add_get("/hls/{name}", _hls)
    app.router.add_get("/icy", _icy)
    app.router.add_get("/flaky", _flaky)
    app.router.add_get("/tone/{tone}.mp3", _tone)
    app.router.add_post("/control/drop", _control_drop)
    app.router.add_post("/control/reset", _control_reset)
    app.router.add_get("/control/state", _control_state)
    app.router.add_get("/health", _health)
    return app


def icy_metadata_block(title: str) -> bytes:
    """Return an ICY metadata block: length byte (in 16-byte units), then the padded text."""
    if not title:
        return b"\x00"
    text = f"StreamTitle='{title}';".encode()
    padded = text + b"\x00" * (-len(text) % 16)
    return bytes([len(padded) // 16]) + padded


def title_index_at(seconds: float) -> int:
    """Return the number of the title playing `seconds` after the service started."""
    return int(seconds // TITLE_PERIOD_S)


def title_at(seconds: float) -> str:
    """Return the stream title playing `seconds` after the service started."""
    return f"Lab Radio {title_index_at(seconds)}"


async def _icy(request: web.Request) -> web.StreamResponse:
    """Stream the MP3 loop like Icecast.

    A burst on connect, then real-time pace, ICY metadata on request.
    """
    state = request.app[STATE]
    with_meta = request.headers.get("Icy-MetaData") == "1"
    headers = {"Content-Type": "audio/mpeg", "icy-name": "Lab Radio", "icy-br": "128"}
    if with_meta:
        headers["icy-metaint"] = str(ICY_METAINT)
    response = web.StreamResponse(headers=headers)
    await response.prepare(request)
    stop = asyncio.Event()
    state.streams.add(stop)
    try:
        await _stream(state, response, stop, with_meta)
    finally:
        state.streams.discard(stop)
    if stop.is_set() and request.transport is not None:
        # an abrupt close, like a radio server dropping the listener
        request.transport.close()
    return response


async def _flaky(request: web.Request) -> web.StreamResponse:
    """Refuse the first request after start or reset with 403, then behave like /icy."""
    if request.app[STATE].take_flaky():
        raise web.HTTPForbidden()
    return await _icy(request)


async def _tone(request: web.Request) -> web.Response:
    """Serve an announce clip as a complete MP3 file."""
    clip = request.app[STATE].clips.get(request.match_info["tone"])
    if clip is None:
        raise web.HTTPNotFound()
    return web.Response(body=clip, content_type="audio/mpeg")


async def _hls(request: web.Request) -> web.FileResponse:
    """Serve the live playlist and its segments from the encoder's output dir."""
    hls_dir = request.app[STATE].hls_dir
    name = request.match_info["name"]
    assert hls_dir is not None
    path = hls_dir / name
    if not HLS_NAME.match(name) or not path.is_file():
        raise web.HTTPNotFound()
    headers = {"Content-Type": HLS_CONTENT_TYPES[path.suffix], "Cache-Control": "no-cache"}
    return web.FileResponse(path, headers=headers)


async def _control_drop(request: web.Request) -> web.Response:
    """End every running /icy and /flaky stream."""
    return web.json_response({"dropped": request.app[STATE].drop()})


async def _control_reset(request: web.Request) -> web.Response:
    """Re-arm the 403 of /flaky."""
    request.app[STATE].flaky_armed = True
    return web.json_response({"flaky_armed": True})


async def _control_state(request: web.Request) -> web.Response:
    """Report the current title, open streams and whether /flaky is armed."""
    state = request.app[STATE]
    return web.json_response(
        {
            "title_index": title_index_at(state.elapsed),
            "connections": len(state.streams),
            "flaky_armed": state.flaky_armed,
        }
    )


async def _health(request: web.Request) -> web.Response:
    """Answer 200 once the MP3 and the first HLS segments exist."""
    state = request.app[STATE]
    if not state.mp3 or not state.hls_ready():
        raise web.HTTPServiceUnavailable()
    return web.Response(text="ok")


async def _stream(
    state: RadioState, response: web.StreamResponse, stop: asyncio.Event, with_meta: bool
) -> None:
    """Write the loop from the shared live position until the client leaves or stop is set."""
    mp3 = state.mp3
    looped = mp3 + mp3[:CHUNK_BYTES]
    live = int(state.elapsed * BYTES_PER_SECOND) // FRAME_BYTES * FRAME_BYTES
    position = (live - BURST_BYTES) % len(mp3)
    t0 = time.monotonic()
    sent = 0
    until_meta = ICY_METAINT
    last_title = ""
    while not stop.is_set():
        chunk = looped[position : position + CHUNK_BYTES]
        position = (position + CHUNK_BYTES) % len(mp3)
        out = bytearray()
        while with_meta and len(chunk) >= until_meta:
            out += chunk[:until_meta]
            chunk = chunk[until_meta:]
            title = title_at(state.elapsed)
            out += icy_metadata_block(title) if title != last_title else b"\x00"
            last_title, until_meta = title, ICY_METAINT
        out += chunk
        until_meta -= len(chunk) if with_meta else 0
        await response.write(bytes(out))
        sent += CHUNK_BYTES
        delay = t0 + (sent - BURST_BYTES) / BYTES_PER_SECOND - time.monotonic()
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass


async def _hls_encoder(app: web.Application) -> AsyncIterator[None]:
    """Run the live HLS encoder for the lifetime of the app."""
    state = app[STATE]
    assert state.hls_dir is not None
    mp3 = state.hls_dir.parent / "tone.mp3"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-re", "-stream_loop", "-1", "-i", str(mp3)]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", str(RATE), "-ac", "2"]
    cmd += ["-f", "hls", "-hls_time", "4", "-hls_list_size", "6"]
    cmd += ["-hls_flags", "delete_segments+append_list"]
    cmd += ["-hls_segment_filename", str(state.hls_dir / "seg%05d.ts")]
    cmd += [str(state.hls_dir / HLS_PLAYLIST)]
    state.hls_process = await asyncio.create_subprocess_exec(*cmd)
    yield
    state.hls_process.terminate()
    await state.hls_process.wait()


def _encode_mp3(manifest_path: Path, tone: str, dest: Path) -> bytes:
    """Synthesize a manifest tone and encode it as headerless 128 kb/s CBR stereo MP3."""
    manifest = Manifest.load(manifest_path)
    track = next(track for track in manifest.tracks if track.id == tone)
    pcm = np.repeat(np.round(synthesize(track, manifest.chirp) * 32767).astype("<i2"), 2)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0"]
    # no ID3 tag or Xing frame, so the file loops as a plain run of audio frames
    cmd += ["-c:a", "libmp3lame", "-b:a", "128k", "-id3v2_version", "0", "-write_xing", "0"]
    cmd += ["-f", "mp3", str(dest)]
    subprocess.run(cmd, input=pcm.tobytes(), check=True)
    return dest.read_bytes()


if __name__ == "__main__":
    main()
