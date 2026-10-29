"""Library checks: the scan follows the music folder, library rows survive a server restart."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

from scenarios.checks.containers import restart_role
from scenarios.checks.context import check
from scenarios.checks.queue import play, stop, tracks

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient
    from music_assistant_models.media_items import Track

    from labctl.analyze import Manifest
    from scenarios.checks.context import CheckContext

SCAN_ISSUES = ("#2083",)
ROWS_RESTART_ISSUES = ("#5310", "#6290")

# the run's writable music folder, at /music/extra in the server
EXTRA_DIR = Path("/run/music-extra")
EXTRA_ARTIST = "Lab Artist Extra"
EXTRA_ALBUM = "Extra Album"
EXTRA_TITLE = "Extra Track"
EXTRA_FILE = EXTRA_DIR / EXTRA_ARTIST / EXTRA_ALBUM / f"01 - {EXTRA_TITLE}.flac"
FILESYSTEM_DOMAIN = "filesystem_local"
SERVER_SERVICE = "ma"
PLAYED_TONE = "a1t1"
SCAN_TIMEOUT_S = 30.0
# a second sync gets this long to add a duplicate
RESCAN_WATCH_S = 10.0
# the server skips play log entries for items played under 5 s
PLAYED_S = 10.0
RECORD_TIMEOUT_S = 10.0
RESTART_TIMEOUT_S = 120.0
RECENTLY_PLAYED_LIMIT = 20
POLL_S = 0.5


@check
async def scan_matches_manifest(ctx: CheckContext) -> None:
    """Check the library holds the tone library, adds a new file once and drops it when gone."""
    expected = len(ctx.tones.tracks)
    [instance_id] = [p.instance_id for p in ctx.client.providers if p.domain == FILESYSTEM_DOMAIN]
    try:
        with ctx.step("count_matches", SCAN_ISSUES):
            count = await _track_count(ctx.client)
            assert count == expected, f"library has {count} tracks, the manifest {expected}"

        with ctx.step("added_file_appears", SCAN_ISSUES):
            await asyncio.to_thread(_write_extra_track, ctx.tones)
            await _sync(ctx.client, instance_id)
            await _until_count(ctx.client, expected + 1, SCAN_TIMEOUT_S)
            found = await _extra_tracks(ctx.client)
            assert len(found) == 1, f"{len(found)} library tracks named {EXTRA_TITLE!r}"

        with ctx.step("rescan_no_duplicates", SCAN_ISSUES):
            await _sync(ctx.client, instance_id)
            deadline = time.monotonic() + RESCAN_WATCH_S
            while time.monotonic() < deadline:
                count = await _track_count(ctx.client)
                assert count == expected + 1, f"library has {count} tracks after a second sync"
                await asyncio.sleep(POLL_S)
            found = await _extra_tracks(ctx.client)
            assert len(found) == 1, f"{len(found)} library tracks named {EXTRA_TITLE!r}"

        # the sync removes the file's provider mapping, and with its last mapping the library
        # track and the album and artist it orphans
        with ctx.step("deleted_file_removed_from_library", SCAN_ISSUES):
            EXTRA_FILE.unlink()
            await _sync(ctx.client, instance_id)
            await _until_count(ctx.client, expected, SCAN_TIMEOUT_S)
            found = await _extra_tracks(ctx.client)
            assert not found, f"library still has {EXTRA_TITLE!r} after its file was deleted"
    finally:
        shutil.rmtree(EXTRA_DIR / EXTRA_ARTIST, ignore_errors=True)


@check
async def rows_survive_restart(ctx: CheckContext, role: str) -> None:
    """Check a favorite and a play log entry are still there after the server restarts."""
    queue_id = ctx.player_id(role)
    [track] = await tracks(ctx, (PLAYED_TONE,))
    try:
        await play(ctx, queue_id, (PLAYED_TONE,), ROWS_RESTART_ISSUES)
        await asyncio.sleep(PLAYED_S)
    finally:
        await stop(ctx, queue_id)
    await ctx.client.music.add_item_to_favorites(track)

    with ctx.step("recorded", ROWS_RESTART_ISSUES):
        await _until_recorded(ctx.client, track)

    async with contextlib.AsyncExitStack() as stack:
        with ctx.step("restarts", ROWS_RESTART_ISSUES):
            restarted = time.time()
            await restart_role(SERVER_SERVICE)
            client = await stack.enter_async_context(ctx.reconnect(RESTART_TIMEOUT_S))
            ctx.record_property("restart_s", round(time.time() - restarted, 1))

        with ctx.step("favorite_kept", ROWS_RESTART_ISSUES):
            assert await _is_favorite(client, track), f"{track.name} is no favorite after restart"

        with ctx.step("playlog_kept", ROWS_RESTART_ISSUES):
            assert await _was_played(client, track), (
                f"{track.name} is not recently played after restart"
            )


def _write_extra_track(tones: Manifest) -> None:
    """Encode the first tone with the extra track's tags into the run's music folder."""
    from tones.generate import encode, synthesize

    source = tones.tracks[0]
    extra = dataclasses.replace(source, artist=EXTRA_ARTIST, album=EXTRA_ALBUM, title=EXTRA_TITLE)
    EXTRA_FILE.parent.mkdir(parents=True, exist_ok=True)
    encode(synthesize(source, tones.chirp), extra, EXTRA_FILE)


async def _sync(client: MusicAssistantClient, instance_id: str) -> None:
    """Start a library sync of the music folder provider."""
    await client.send_command("music/sync", providers=[instance_id])


async def _track_count(client: MusicAssistantClient) -> int:
    """Return the number of tracks in the library."""
    count: int = await client.send_command("music/tracks/count")
    return count


async def _until_count(client: MusicAssistantClient, expected: int, timeout: float) -> None:
    """Wait until the library holds `expected` tracks."""
    deadline = time.monotonic() + timeout
    while (count := await _track_count(client)) != expected:
        assert time.monotonic() < deadline, (
            f"library has {count} tracks after {timeout:.0f} s, expected {expected}"
        )
        await asyncio.sleep(POLL_S)


async def _extra_tracks(client: MusicAssistantClient) -> list[Track]:
    """Library tracks named like the extra track."""
    found = await client.music.get_library_tracks(search=EXTRA_TITLE)
    return [track for track in found if track.name == EXTRA_TITLE]


async def _until_recorded(client: MusicAssistantClient, track: Track) -> None:
    """Wait until the track is a favorite and in the play log."""
    deadline = time.monotonic() + RECORD_TIMEOUT_S
    while True:
        favorite, played = await _is_favorite(client, track), await _was_played(client, track)
        if favorite and played:
            return
        assert time.monotonic() < deadline, (
            f"{track.name} after {RECORD_TIMEOUT_S:.0f} s: favorite {favorite}, "
            f"recently played {played}"
        )
        await asyncio.sleep(POLL_S)


async def _is_favorite(client: MusicAssistantClient, track: Track) -> bool:
    """Whether the library track is a favorite."""
    return (await client.music.get_track(track.item_id, "library")).favorite


async def _was_played(client: MusicAssistantClient, track: Track) -> bool:
    """Whether the track is among the recently played tracks, fully played or not."""
    played = await client.music.recently_played(
        limit=RECENTLY_PLAYED_LIMIT, media_types=[MediaType.TRACK], fully_played_only=False
    )
    return any(item.name == track.name for item in played)
