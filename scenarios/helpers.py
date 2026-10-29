"""Helpers shared by music scenarios: track selection, queue events, server log, Soloist engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from music_assistant_models.enums import MediaType, SortField

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import Track

    from labctl.analyze import Segment

# the account golden, mounted read-only into the tests container of a music run
DATA_DIR = Path("/data")
MA_LOG = DATA_DIR / "musicassistant.log"
# the level field after the timestamp of a server log line
ERROR_LINE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ (ERROR|CRITICAL) ")
MIN_TRACK_S = 60
MAX_TRACK_S = 240
LIBRARY_LIMIT = 500
# fallback for sets whose library has too few short tracks; every service has these
SEARCH_QUERY = "The Beatles"
SEARCH_LIMIT = 50
ENGINE_COMMAND_TIMEOUT_S = 5.0


async def pick_tracks(client: MusicAssistantClient, count: int) -> list[Track]:
    """Return `count` available tracks of 1 to 4 minutes, library first (by name), else search."""
    library = await client.music.get_library_tracks(limit=LIBRARY_LIMIT, sort_field=SortField.NAME)
    picked = _fitting(library, count)
    if len(picked) < count:
        found = await client.music.search(SEARCH_QUERY, [MediaType.TRACK], limit=SEARCH_LIMIT)
        picked = _fitting((track for track in found.tracks if track.provider != "library"), count)
    if len(picked) < count:
        bounds = f"{MIN_TRACK_S} and {MAX_TRACK_S} s"
        pytest.fail(f"found {len(picked)} of {count} tracks between {bounds}")
    return picked


def describe(track: Track) -> str:
    """One-line description of a track for test output."""
    return f"{track.uri} {track.name!r} ({track.duration} s)"


def queue_field(event: MassEvent, name: str) -> Any:
    """Return a field of the queue carried by a QUEUE_UPDATED event."""
    return event.data[name]


def queue_playing(queue_id: str, index: int) -> Callable[[MassEvent], bool]:
    """Match a QUEUE_UPDATED event of `queue_id` playing the item at `index`."""

    def playing(event: MassEvent) -> bool:
        return (
            event.object_id == queue_id
            and queue_field(event, "state") == "playing"
            and queue_field(event, "current_index") == index
        )

    return playing


def silences(timeline: Iterable[Segment], t0: float, t1: float) -> list[Segment]:
    """Return silence segments overlapping [t0, t1], clipped to it."""
    return [
        seg._replace(t0=max(seg.t0, t0), t1=min(seg.t1, t1))
        for seg in timeline
        if seg.kind == "silence" and seg.t1 > t0 and seg.t0 < t1
    ]


def ma_log_size() -> int:
    """Return the current size of the server log, to read what is written after it."""
    return MA_LOG.stat().st_size


def ma_log_since(offset: int) -> list[str]:
    """Return the server log lines written after `offset`."""
    with MA_LOG.open("rb") as log:
        log.seek(offset)
        return log.read().decode(errors="replace").splitlines()


def ma_log_errors(offset: int) -> list[str]:
    """Return the ERROR and CRITICAL server log lines written after `offset`."""
    return [line for line in ma_log_since(offset) if ERROR_LINE.match(line)]


async def until(condition: Callable[[], bool], timeout: float, poll: float = 0.5) -> None:
    """Poll a condition until it holds, failing the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"condition {condition.__name__} not met within {timeout:.1f} s")
        await asyncio.sleep(poll)


async def soloist_command(instance_id: str, command: str) -> None:
    """Send a command to the provider's running Soloist engine over its own WebSocket."""
    # the engine publishes its loopback endpoint in the provider's soloist data dir
    engine_dir = DATA_DIR / "spotify" / instance_id / "soloist-data"
    addr = (engine_dir / "ws.addr").read_text().strip()
    port = int((engine_dir / "ws.port").read_text().strip())
    async with (
        asyncio.timeout(ENGINE_COMMAND_TIMEOUT_S),
        aiohttp.ClientSession() as session,
        session.ws_connect(f"ws://{addr}:{port}/") as ws,
    ):
        await ws.send_json({"type": "command", "command": command})
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(message.data)
            if frame.get("type") == "command_result" and frame.get("command") == command:
                return
    pytest.fail(f"Soloist engine closed the socket before acknowledging {command}")


def _fitting(tracks: Iterable[Track], count: int) -> list[Track]:
    """First `count` distinct available tracks within the duration bounds."""
    picked: list[Track] = []
    for track in tracks:
        if (
            track.duration is not None
            and MIN_TRACK_S <= track.duration <= MAX_TRACK_S
            and any(mapping.available for mapping in track.provider_mappings)
            and all(track.uri != other.uri for other in picked)
        ):
            picked.append(track)
        if len(picked) == count:
            break
    return picked
