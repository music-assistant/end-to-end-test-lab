"""Music checks on a real streaming account: track boundaries, the last track, external pause."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import EventType, QueueOption

from scenarios.checks.context import check
from scenarios.helpers import (
    describe,
    ma_log_errors,
    ma_log_since,
    ma_log_size,
    pick_tracks,
    queue_field,
    queue_playing,
    silences,
    soloist_command,
)

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import MediaItemType, Track

    from scenarios.checks.context import CheckContext

CONTINUITY_ISSUES = ("#6860",)
EXTERNAL_PAUSE_ISSUES = ("#6752", "#7117")
LAST_TRACK_ISSUES = ("#6860", "#6667", "#6222")

# the seek leaves this much of the track, so the check sees its natural end quickly
TAIL_S = 20
START_TIMEOUT_S = 30.0
# the track plays this long before the seek, so the seek does not race the start
PLAYED_BEFORE_SEEK_S = 3.0
SEEK_TIMEOUT_S = 15.0
ADVANCE_MARGIN_S = 15.0
END_MARGIN_S = 15.0
TRACK2_LISTEN_S = 8.0
# reported duration, seek and elapsed time are whole seconds and a fade out can dip below -60 dB
END_TOLERANCE_S = 3.0
MAX_TRACK_SILENCE_S = 1.0
MAX_BOUNDARY_GAP_S = 3.0
STOPPED_STATES = ("idle", "paused")
# sound later than this after the expected end is the track played again
REPLAY_AFTER_S = 5.0
REPLAY_WATCH_S = 10.0
PAUSE_AFTER_S = 10.0
STOP_WITHIN_S = 3.0
IDLE_TIMEOUT_S = 10.0
# the resume position may trail the elapsed time at the pause by what the player had buffered
RESUME_TOLERANCE_S = 3.0


@check
async def continuity(ctx: CheckContext, role: str) -> None:
    """Check item 1 plays to its end and the queue moves on to item 2 without stopping."""
    player_id = ctx.player_id(role)
    first, second = await pick_tracks(ctx.client, 2)
    ctx.record_property("tracks", f"{describe(first)} then {describe(second)}")
    log_offset = ma_log_size()
    try:
        resumed, expected_end = await _seek_to_tail(
            ctx, player_id, [first, second], CONTINUITY_ISSUES
        )

        with ctx.step("advances", CONTINUITY_ISSUES):
            update = await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                lambda e: (
                    e.object_id == player_id
                    and (
                        queue_field(e, "current_index") == 1
                        or queue_field(e, "state") in STOPPED_STATES
                    )
                ),
                TAIL_S + ADVANCE_MARGIN_S,
                since=resumed,
            )
            advanced = time.time()
            assert queue_field(update, "current_index") == 1, (
                f"queue {queue_field(update, 'state')} on item 1 at "
                f"{queue_field(update, 'elapsed_time'):.1f} of {first.duration} s "
                "instead of advancing to item 2"
            )
            stops = [
                f"{recorded.event.event} {recorded.time - resumed:.1f} s after the seek"
                for recorded in ctx.events.events
                if resumed <= recorded.time <= advanced and _stopped(recorded.event, player_id)
            ]
            assert not stops, f"queue or player stopped before item 2: {stops}"

        listen_end = max(advanced, expected_end) + TRACK2_LISTEN_S
        await asyncio.sleep(listen_end - time.time())
        received = ctx.capture(role)
        timeline = received.timeline()
        with ctx.step("plays_to_end", CONTINUITY_ISSUES):
            assert received.ended is None, f"{role} capture ended at {received.ended}"
            tail = silences(timeline, resumed, expected_end - END_TOLERANCE_S)
            assert max((seg.duration for seg in tail), default=0.0) < MAX_TRACK_SILENCE_S, (
                f"item 1 went silent before its end ({expected_end:.1f}): {tail}"
            )

        with ctx.step("no_boundary_gap", CONTINUITY_ISSUES):
            boundary = silences(timeline, expected_end - END_TOLERANCE_S, listen_end)
            gap = max((seg.duration for seg in boundary), default=0.0)
            ctx.record_property("boundary_gap_s", f"{gap:.2f}")
            assert gap < MAX_BOUNDARY_GAP_S, (
                f"{gap:.1f} s gap between item 1 and item 2 (end {expected_end:.1f}): {boundary}"
            )

        with ctx.step("no_stop_logged", CONTINUITY_ISSUES):
            stop_lines = [line for line in ma_log_since(log_offset) if "stopping queue" in line]
            assert not stop_lines, f"server log: {stop_lines}"
    finally:
        await ctx.client.player_queues.stop(player_id)


@check
async def external_pause(ctx: CheckContext, role: str) -> None:
    """Check pausing the Soloist engine mid-track stops the queue with a resume position."""
    spotify = [p for p in ctx.client.providers if p.domain == "spotify"]
    if not spotify:
        pytest.skip("needs a Spotify account set (Soloist backend); this golden has no spotify")
    player_id = ctx.player_id(role)
    [track] = await pick_tracks(ctx.client, 1)
    ctx.record_property("track", describe(track))

    started = time.time()
    try:
        with ctx.step("starts", EXTERNAL_PAUSE_ISSUES):
            await ctx.client.player_queues.play_media(player_id, track, QueueOption.REPLACE)
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(player_id, 0),
                START_TIMEOUT_S,
                since=started,
            )
        await asyncio.sleep(PAUSE_AFTER_S)
        queue = ctx.client.player_queues.get(player_id)
        assert queue is not None
        elapsed = queue.corrected_elapsed_time
        paused = time.time()
        await soloist_command(spotify[0].instance_id, "pause")

        with ctx.step("stops", EXTERNAL_PAUSE_ISSUES):
            try:
                # a stop sets the resume position at once; idle follows when the player confirms
                await ctx.events.wait_for(
                    EventType.QUEUE_UPDATED,
                    lambda e: (
                        e.object_id == player_id
                        and (queue_field(e, "resume_pos") > 0 or queue_field(e, "state") == "idle")
                    ),
                    STOP_WITHIN_S,
                    since=paused,
                )
            except AssertionError:
                pytest.fail(f"queue not stopped within {STOP_WITHIN_S} s of the engine pause")
            ctx.record_property("stop_s", f"{time.time() - paused:.2f}")

        with ctx.step("keeps_resume_position", EXTERNAL_PAUSE_ISSUES):
            stopped = await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                lambda e: e.object_id == player_id and queue_field(e, "state") == "idle",
                IDLE_TIMEOUT_S,
                since=paused,
            )
            resume_pos = queue_field(stopped, "resume_pos")
            assert queue_field(stopped, "current_index") == 0, "queue moved off the paused item"
            assert resume_pos >= elapsed - RESUME_TOLERANCE_S, (
                f"resume position {resume_pos} s, paused at {elapsed:.1f} s"
            )
    finally:
        await ctx.client.player_queues.stop(player_id)


@check
async def last_track_ends_clean(ctx: CheckContext, role: str) -> None:
    """Check a single queued track plays to its end, goes idle and nothing plays after."""
    player_id = ctx.player_id(role)
    [track] = await pick_tracks(ctx.client, 1)
    ctx.record_property("track", describe(track))
    log_offset = ma_log_size()
    try:
        resumed, expected_end = await _seek_to_tail(ctx, player_id, [track], LAST_TRACK_ISSUES)

        with ctx.step("ends_idle", LAST_TRACK_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                lambda e: e.object_id == player_id and queue_field(e, "state") == "idle",
                TAIL_S + END_MARGIN_S,
                since=resumed,
            )
        listen_end = expected_end + REPLAY_AFTER_S + REPLAY_WATCH_S
        await asyncio.sleep(max(0.0, listen_end - time.time()))

        with ctx.step("no_pause", LAST_TRACK_ISSUES):
            paused = [
                f"{recorded.event.event} {recorded.time - resumed:.1f} s after the seek"
                for recorded in ctx.events.events
                if recorded.time >= resumed and _paused(recorded.event, player_id)
            ]
            assert not paused, f"queue or player paused instead of ending: {paused}"

        received = ctx.capture(role)
        timeline = received.timeline()
        with ctx.step("plays_to_end", LAST_TRACK_ISSUES):
            assert received.ended is None, f"{role} capture ended at {received.ended}"
            tail = silences(timeline, resumed, expected_end - END_TOLERANCE_S)
            assert max((seg.duration for seg in tail), default=0.0) < MAX_TRACK_SILENCE_S, (
                f"the track went silent before its end ({expected_end:.1f}): {tail}"
            )

        with ctx.step("no_replay", LAST_TRACK_ISSUES):
            replay = [
                seg
                for seg in timeline
                if seg.kind != "silence" and seg.t1 > expected_end + REPLAY_AFTER_S
            ]
            assert not replay, f"sound after the track ended ({expected_end:.1f}): {replay}"

        with ctx.step("no_errors_logged", LAST_TRACK_ISSUES):
            errors = ma_log_errors(log_offset)
            assert not errors, f"server log errors: {errors}"
    finally:
        await ctx.client.player_queues.stop(player_id)


async def _seek_to_tail(
    ctx: CheckContext, player_id: str, tracks: list[Track], issues: tuple[str, ...]
) -> tuple[float, float]:
    """Play `tracks`, seek item 1 to its tail and return when it resumed and its expected end."""
    first = tracks[0]
    media: list[MediaItemType] = [*tracks]
    assert first.duration is not None
    started = time.time()
    with ctx.step("starts", issues):
        await ctx.client.player_queues.play_media(player_id, media, QueueOption.REPLACE)
        await ctx.events.wait_for(
            EventType.QUEUE_UPDATED, queue_playing(player_id, 0), START_TIMEOUT_S, since=started
        )
    await asyncio.sleep(PLAYED_BEFORE_SEEK_S)

    with ctx.step("seeks", issues):
        position = first.duration - TAIL_S
        seeked = time.time()
        await ctx.client.player_queues.seek(player_id, position)
        at_seek = await ctx.events.wait_for(
            EventType.QUEUE_TIME_UPDATED,
            _time_from(player_id, position - 1),
            SEEK_TIMEOUT_S,
            since=seeked,
        )
    resumed = time.time()
    return resumed, resumed + first.duration - float(at_seek.data)


def _time_from(player_id: str, minimum: float) -> Callable[[MassEvent], bool]:
    """Match a QUEUE_TIME_UPDATED event of `player_id` at `minimum` seconds or later."""

    def reached(event: MassEvent) -> bool:
        return event.object_id == player_id and float(event.data) >= minimum

    return reached


def _stopped(event: MassEvent, player_id: str) -> bool:
    """Whether an event shows the queue or the player of `player_id` idle or paused."""
    if event.object_id != player_id:
        return False
    if event.event == EventType.QUEUE_UPDATED:
        return bool(event.data["state"] in STOPPED_STATES)
    if event.event == EventType.PLAYER_UPDATED:
        return bool(event.data.get("playback_state") in STOPPED_STATES)
    return False


def _paused(event: MassEvent, player_id: str) -> bool:
    """Whether an event shows the queue or the player of `player_id` paused."""
    if event.object_id != player_id:
        return False
    if event.event == EventType.QUEUE_UPDATED:
        return bool(event.data["state"] == "paused")
    if event.event == EventType.PLAYER_UPDATED:
        return bool(event.data.get("playback_state") == "paused")
    return False
