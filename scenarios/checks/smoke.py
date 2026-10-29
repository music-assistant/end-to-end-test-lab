"""Smoke checks: a player plays a tone and stops, heard on a capture or reported by a device."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, PlaybackState, QueueOption

from scenarios.checks.context import check
from scenarios.helpers import until

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from scenarios.checks.context import CheckContext

PLAYING_TIMEOUT_S = 10.0
TONE_WITHIN_S = 10.0
# enough tone to be sure it is the track and not a stray window
TONE_MIN_S = 1.0
SILENT_WITHIN_S = 2.0
# silence observed past the deadline before the capture counts as silent
SILENT_MIN_S = 1.0
POLL_S = 0.5

DEVICE_TONE = "a1t4"
# real devices buffer and start slower than the virtual players
DEVICE_PLAYING_TIMEOUT_S = 20.0
DEVICE_IDLE_TIMEOUT_S = 10.0


@check
async def play_stop_heard(ctx: CheckContext, role: str) -> None:
    """Check tone track A reaches the role's capture after play and stops after stop."""
    player_id = ctx.player_id(role)
    tone = ctx.tones.tracks[0]
    [track] = await ctx.client.music.get_library_tracks(search=tone.title, limit=1)

    def playing() -> bool:
        player = ctx.client.players.get(player_id)
        return player is not None and player.playback_state == PlaybackState.PLAYING

    def tone_heard() -> bool:
        return any(
            seg.kind == tone.id and seg.t0 <= played + TONE_WITHIN_S and seg.duration >= TONE_MIN_S
            for seg in ctx.capture(role).timeline()
        )

    played = time.time()
    try:
        with ctx.step("playing"):
            await ctx.client.player_queues.play_media(player_id, track)
            await until(playing, PLAYING_TIMEOUT_S, POLL_S)
        with ctx.step("tone_heard"):
            await until(tone_heard, TONE_WITHIN_S + TONE_MIN_S + POLL_S, POLL_S)
    finally:
        stopped = time.time()
        await ctx.client.players.stop(player_id)

    await asyncio.sleep(SILENT_WITHIN_S + SILENT_MIN_S)
    after_stop = ctx.capture(role)
    with ctx.step("silent_after_stop"):
        # unwritten time counts as silence, so a dead capture sidecar would pass the check below
        assert after_stop.ended is None, (
            f"{role} capture ended at {after_stop.ended}, stop {stopped}"
        )
        sound = [
            seg
            for seg in after_stop.timeline()
            if seg.kind != "silence" and seg.t1 > stopped + SILENT_WITHIN_S
        ]
        assert not sound, f"{role} still has sound {SILENT_WITHIN_S} s after stop: {sound}"


@check
async def play_stop_reported(ctx: CheckContext, device: str) -> None:
    """Check the device reports playing after play and idle after stop; records both latencies."""
    player_id = ctx.player_id(device)
    title = next(tone.title for tone in ctx.tones.tracks if tone.id == DEVICE_TONE)
    [track] = await ctx.client.music.get_library_tracks(search=title, limit=1)

    since = time.time()
    try:
        with ctx.step("playing"):
            await ctx.client.player_queues.play_media(player_id, track, QueueOption.REPLACE)
            await ctx.events.wait_for(
                EventType.PLAYER_UPDATED,
                _state(player_id, "playing"),
                DEVICE_PLAYING_TIMEOUT_S,
                since,
            )
            ctx.record_property("playing_s", round(time.time() - since, 2))
    finally:
        # a real speaker keeps playing after a failed check unless it is stopped
        since = time.time()
        await ctx.client.player_queues.stop(player_id)
    with ctx.step("idle"):
        await ctx.events.wait_for(
            EventType.PLAYER_UPDATED, _state(player_id, "idle"), DEVICE_IDLE_TIMEOUT_S, since
        )
        ctx.record_property("idle_s", round(time.time() - since, 2))


def _state(player_id: str, state: str) -> Callable[[MassEvent], bool]:
    """Match a PLAYER_UPDATED event of `player_id` in playback state `state`."""

    def matching(event: MassEvent) -> bool:
        return event.object_id == player_id and event.data.get("playback_state") == state

    # names the state in the timeout message
    matching.__name__ = state
    return matching
