"""Player checks: sync groups, volume, pause and announcements."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import EventType, PlaybackState, PlayerFeature, QueueOption
from music_assistant_models.errors import PlayerUnavailableError

from scenarios.checks.containers import restart_role, start_role, stop_role
from scenarios.checks.queue import (
    CAPTURE_SETTLE_S,
    COMMAND_TIMEOUT_S,
    HEARD_MIN_S,
    HEARD_TIMEOUT_S,
    MAX_RESUME_REWIND_S,
    POLL_S,
    START_TIMEOUT_S,
    TAIL_TONE_S,
    describe,
    duration,
    play,
    queue_in,
    queue_left_playing,
    stop,
    tone_runs,
    tracks,
)
from scenarios.helpers import queue_playing, silences, until

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import Player

    from labctl.analyze import Segment
    from scenarios.checks.context import CheckContext

SYNC_ISSUES = ("#4460", "#5756", "#6550")
MEMBER_REMOVED_ISSUES = ("#5559", "#6016", "#5431")
GROUP_SKIP_ISSUES = ("server#6611",)
GROUP_ANNOUNCE_ISSUES = ("#6341", "#6159")
REJOIN_ISSUES = ("#6191", "#5880")
RESTART_UNGROUP_ISSUES = ("#6191",)
MUTED_MEMBER_ISSUES = ("#5098",)
VOLUME_SET_ISSUES = ("#5638",)
GROUP_VOLUME_ISSUES = ("#4324", "#5202")
VOLUME_PAUSE_ISSUES = ("#6633",)
PAUSE_ISSUES = ("#6614", "#5276", "#2794")
DISCONNECT_ISSUES = ("#6581", "#6064")
CLIENT_QUEUE_ISSUES = ("server#7265",)
ANNOUNCE_ISSUES = ("#6179", "#6616", "#4377", "#4791")
ANNOUNCE_IDLE_ISSUES = ("#3468",)
ANNOUNCE_SHORT_ISSUES = ("#4008", "#3373", "#4001", "#6663")

TWO_TRACKS = ("a1t1", "a1t2")
A1_TONES = ("a1t1", "a1t2", "a1t3", "a1t4")
ALBUM = ("a2t1", "a2t2", "a2t3")
SOLO_TONE = "a2t1"
LONG_TONE = "a1t4"
CLIP = "a3t1"
SHORT_CLIP = "a4t1"
FULL_VOLUME = 100
SYNCGROUP_PROVIDER = "sync_group"

GROUP_TIMEOUT_S = 10.0
UNGROUP_TIMEOUT_S = 10.0
# regrouping right after an ungroup races the server's sync-group reform
GROUP_SETTLE_S = 1.0
UNGROUP_SETTLE_S = 1.0
SYNC_HEARD_S = 15.0
# start latency on top of the 15 s that must be heard
SYNC_HEARD_TIMEOUT_S = SYNC_HEARD_S + 10.0
# two speakers over 20 ms apart sound like an echo; measured 1 to 4 ms on every lab pair
MAX_SYNC_OFFSET_S = 0.02
ADVANCE_MARGIN_S = 15.0
PLAYED_BEFORE_UNGROUP_S = 5.0
LEADER_WATCH_S = 10.0
MAX_LEADER_GAP_S = 1.0
MEMBER_IDLE_S = 5.0
MEMBER_SILENT_WITHIN_S = 3.0
SOLO_HEARD_S = 10.0
PLAYED_BEFORE_NEXT_S = 5.0
SKIP_HEARD_S = 10.0
PLAYED_BEFORE_ANNOUNCE_S = 5.0
# bounds the announcement call, which only returns once the clip played and playback is restored
ANNOUNCE_TIMEOUT_S = 60.0
CLIP_HEARD_S = 8.0
REGROUP_S = 10.0
GROUP_RESUME_HEARD_S = 15.0
DROP_TIMEOUT_S = 30.0
RETURN_TIMEOUT_S = 60.0
REJOIN_TIMEOUT_S = 30.0
REJOIN_HEARD_S = 20.0
MUTED_GROUP_VOLUME = 70
MUTE_SETTLE_S = 3.0
MUTED_SILENT_S = 5.0
VOLUME_TOLERANCE = 1
LEVEL_SPAN_S = 3.0
# skips the fade-in at the start of a heard tone
LEVEL_LEAD_S = 0.5
LOWER_VOLUME = 40
VOLUME_EFFECT_S = 3.0
# the level after a volume change is read over the last second of VOLUME_EFFECT_S
LEVEL_READ_S = 1.0
LEVEL_SETTLE_S = 0.5
MIN_DROP_DB = 6.0
MAX_RESTORE_DIFF_DB = 3.0
GROUP_LEADER_VOLUME = 80
GROUP_MEMBER_VOLUME = 40
GROUP_TARGET = 50
GROUP_VOLUME_S = 3.0
PAUSE_TEST_VOLUME = 70
PLAYED_BEFORE_PAUSE_S = 3.0
VOLUME_SAMPLE_AFTER_S = 1.0
PLAYED_BEFORE_HOLD_S = 5.0
# sound may outlive a pause by the player's buffer
PAUSE_SILENT_WITHIN_S = 2.0
# the queue stops a player paused for 30 s, so the hold stays well below that
HOLD_S = 20.0
RESUME_TIMEOUT_S = 5.0
RESUME_READ_S = 1.0
MAX_RESUME_ADVANCE_S = 4.0
DISCONNECT_TIMEOUT_S = 30.0
# the server sends a dropped queue's final idle update with the unregister
CLIENT_QUEUE_IDLE_S = 10.0
ANNOUNCE_MUSIC_VOLUME = 60
ANNOUNCE_VOLUME = 90
MUSIC_RESUME_S = 10.0
VOLUME_RESTORE_S = 5.0
POSITION_READ_S = 2.0
# about 5 s played before the announcement, plus a little
MIN_RESUMED_POSITION_S = 4.0
MAX_RESUMED_POSITION_S = 14.0
QUIET_AFTER_S = 5.0
CLIP_TAIL_S = 1.0
SHORT_CLIP_HEARD_S = 1.0
# two back-to-back clips are at least this far apart, while one clip is never split this long
CLIP_MERGE_GAP_S = 0.3
CHIME_MIN_S = 0.3


def partner(role: str) -> str:
    """Return the role of the same kind with the next index: `slim-1` -> `slim-2`."""
    kind, index = role.rsplit("-", 1)
    return f"{kind}-{int(index) + 1}"


async def group_plays_in_sync(ctx: CheckContext, role: str) -> None:
    """Check a two-player group plays the same audio in sync and advances on both."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    first, second = TWO_TRACKS
    try:
        await _group(ctx, leader, member, SYNC_ISSUES)
        started = await _play_group(ctx, leader, TWO_TRACKS, SYNC_ISSUES)
        with ctx.step("both_play", SYNC_ISSUES):
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (first,), started, SYNC_HEARD_S, SYNC_HEARD_TIMEOUT_S)

        with ctx.step("both_advance", SYNC_ISSUES):
            deadline = started + duration(ctx, first) + ADVANCE_MARGIN_S
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (second,), started, HEARD_MIN_S, _left(deadline))

        # after the advance so track 1's end and track 2's start markers pair as well; a player
        # syncing at stream start can garble its first marker (runs 20261028-084311-c82e on snap,
        # 20261028-092954-6f4a on slim)
        with ctx.step("in_sync", SYNC_ISSUES):
            offset = ctx.capture(role).sync_offset(ctx.capture(other))
            assert offset is not None, f"no offset the markers on {role} and {other} agree on"
            ctx.record_property("sync_offset_s", round(offset, 3))
            assert abs(offset) <= MAX_SYNC_OFFSET_S, f"{other} lags {role} by {offset:.3f} s"
    finally:
        await _teardown(ctx, leader, member)


async def member_removed_keeps_leader(ctx: CheckContext, role: str) -> None:
    """Check ungrouping a member leaves the leader playing and the member idle and usable."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)

    def member_left_playing() -> bool:
        return not _is_playing(ctx, member)

    try:
        await _group(ctx, leader, member, MEMBER_REMOVED_ISSUES)
        await _play_group(ctx, leader, TWO_TRACKS, MEMBER_REMOVED_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_UNGROUP_S)

        ungrouped = time.time()
        await ctx.client.players.ungroup(member)
        # watched from the ungroup on, while the leader is listened to
        idle = asyncio.create_task(_when(member_left_playing, MEMBER_IDLE_S))
        try:
            with ctx.step("leader_keeps_playing", MEMBER_REMOVED_ISSUES):
                watched = ungrouped + LEADER_WATCH_S
                await _sleep_until(watched + CAPTURE_SETTLE_S)
                timeline = ctx.capture(role).timeline()
                gaps = [
                    seg
                    for seg in silences(timeline, ungrouped, watched)
                    if seg.duration >= MAX_LEADER_GAP_S
                ]
                assert not gaps, f"leader silent after the ungroup: {describe(gaps, ungrouped)}"
                queue = ctx.client.player_queues.get(leader)
                state = queue.state if queue is not None else None
                assert state == PlaybackState.PLAYING, f"leader queue {state} after the ungroup"

            with ctx.step("member_goes_idle", MEMBER_REMOVED_ISSUES):
                left = await idle
                ctx.record_property("member_idle_s", round(left - ungrouped, 1))
                sound = _sound(ctx, other, ungrouped + MEMBER_SILENT_WITHIN_S, watched)
                assert not sound, f"member sounds after the ungroup: {describe(sound, ungrouped)}"
                await _until_ungrouped(ctx, leader, member)
        finally:
            idle.cancel()
        await asyncio.sleep(UNGROUP_SETTLE_S)

        with ctx.step("member_plays_solo", MEMBER_REMOVED_ISSUES):
            solo = time.time()
            media = await tracks(ctx, (SOLO_TONE,))
            await ctx.client.player_queues.play_media(member, media, QueueOption.REPLACE)
            await _heard(ctx, other, (SOLO_TONE,), solo, HEARD_MIN_S, SOLO_HEARD_S)
    finally:
        await stop(ctx, member)
        await _teardown(ctx, leader, member)


async def group_skip_intact(ctx: CheckContext, role: str) -> None:
    """Check next on a group skips one track on both players and keeps the group together."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    second = ALBUM[1]
    try:
        await _group(ctx, leader, member, GROUP_SKIP_ISSUES)
        await _play_group(ctx, leader, ALBUM, GROUP_SKIP_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_NEXT_S)

        skipped = time.time()
        await ctx.client.player_queues.next(leader)
        with ctx.step("both_skip", GROUP_SKIP_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(leader, 1),
                COMMAND_TIMEOUT_S,
                since=skipped,
            )
            for heard_on in (role, other):
                await _heard(
                    ctx, heard_on, (second,), skipped, HEARD_MIN_S, _left(skipped + SKIP_HEARD_S)
                )

        with ctx.step("group_intact", GROUP_SKIP_ISSUES):
            follower, head = _player(ctx, member), _player(ctx, leader)
            assert follower.synced_to == leader, f"member synced to {follower.synced_to}"
            assert member in head.group_members, f"leader members {list(head.group_members)}"
    finally:
        await _teardown(ctx, leader, member)


async def group_announce_restores(ctx: CheckContext, role: str) -> None:
    """Check an announcement on a group member plays and the group resumes the interrupted track."""
    other = partner(role)
    interrupted = TWO_TRACKS[0]
    leader, member = ctx.player_id(role), ctx.player_id(other)
    try:
        await _group(ctx, leader, member, GROUP_ANNOUNCE_ISSUES)
        await _play_group(ctx, leader, TWO_TRACKS, GROUP_ANNOUNCE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_ANNOUNCE_S)

        with ctx.step("announces", GROUP_ANNOUNCE_ISSUES):
            announced = time.time()
            await _announce(ctx, member, CLIP)
            returned = time.time()
            clip = await _heard(ctx, other, (CLIP,), announced, CLIP_HEARD_S, CAPTURE_SETTLE_S)

        with ctx.step("group_restored", GROUP_ANNOUNCE_ISSUES):
            await _until_synced(ctx, member, leader, REGROUP_S)

        with ctx.step("playback_resumes", GROUP_ANNOUNCE_ISSUES):
            deadline = returned + GROUP_RESUME_HEARD_S
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (interrupted,), clip.t1, HEARD_MIN_S, _left(deadline))
    finally:
        await _teardown(ctx, leader, member)


async def member_rejoins(ctx: CheckContext, role: str) -> None:
    """Check a static syncgroup member whose player restarts drops out, returns and rejoins."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    group_id: str | None = None
    try:
        with ctx.step("groups", REJOIN_ISSUES):
            group = await ctx.client.players.create_group_player(
                SYNCGROUP_PROVIDER, f"lab rejoin {role}", [leader, member], dynamic=False
            )
            group_id = group.player_id
            await _until_player(
                ctx,
                group_id,
                f"static members {leader} and {member}",
                lambda p: set(p.static_group_members) == {leader, member},
                GROUP_TIMEOUT_S,
            )
        started = await _play_group(ctx, group_id, (LONG_TONE,), REJOIN_ISSUES)
        # the restart must hit a member that really plays
        with ctx.step("both_play", REJOIN_ISSUES):
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (LONG_TONE,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)

        returned = await _restart_member(ctx, other, REJOIN_ISSUES)
        with ctx.step("rejoins", REJOIN_ISSUES):
            await _until_rejoined(ctx, group_id, member, REJOIN_TIMEOUT_S)
            await _heard(ctx, other, (LONG_TONE,), returned, HEARD_MIN_S, REJOIN_HEARD_S)
    finally:
        if group_id is not None:
            await _remove_syncgroup(ctx, group_id, leader, member)


async def member_restart_ungroups(ctx: CheckContext, role: str) -> None:
    """Check a native group member whose player restarts leaves the group; the leader plays on."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)

    def ungrouped() -> bool:
        follower, head = _player(ctx, member), _player(ctx, leader)
        return follower.synced_to != leader and member not in head.group_members

    try:
        await _group(ctx, leader, member, RESTART_UNGROUP_ISSUES)
        started = await _play_group(ctx, leader, (LONG_TONE,), RESTART_UNGROUP_ISSUES)
        with ctx.step("both_play", RESTART_UNGROUP_ISSUES):
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (LONG_TONE,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)

        returned = await _restart_member(ctx, other, RESTART_UNGROUP_ISSUES)
        with ctx.step("ungrouped", RESTART_UNGROUP_ISSUES):
            deadline = returned + REJOIN_TIMEOUT_S
            while not ungrouped() and time.time() < deadline:
                await asyncio.sleep(POLL_S)
            follower, head = _player(ctx, member), _player(ctx, leader)
            sound = "sounds" if _sound(ctx, other, returned, time.time()) else "silent"
            assert ungrouped(), (
                f"member still synced to {follower.synced_to}, leader members"
                f" {list(head.group_members)}, member {sound} since its return"
            )
            ungrouped_at = time.time()

        with ctx.step("leader_plays", RESTART_UNGROUP_ISSUES):
            await _heard(ctx, role, (LONG_TONE,), ungrouped_at, HEARD_MIN_S, HEARD_TIMEOUT_S)
    finally:
        await _teardown(ctx, leader, member)


async def muted_member_stays_muted(ctx: CheckContext, role: str) -> None:
    """Check a group volume change leaves a muted member muted and changes the others."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    if PlayerFeature.VOLUME_MUTE not in _player(ctx, member).supported_features:
        pytest.skip(f"{other} has no mute")
    try:
        await _group(ctx, leader, member, MUTED_MEMBER_ISSUES)
        started = await _play_group(ctx, leader, (LONG_TONE,), MUTED_MEMBER_ISSUES)
        # silence only proves the mute when the member sounded before it
        with ctx.step("both_play", MUTED_MEMBER_ISSUES):
            for heard_on in (role, other):
                await _heard(ctx, heard_on, (LONG_TONE,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)

        await ctx.client.players.volume_mute(member, True)
        changed = time.time()
        await ctx.client.players.group_volume(leader, MUTED_GROUP_VOLUME)
        with ctx.step("stays_muted", MUTED_MEMBER_ISSUES):
            await _sleep_until(changed + MUTE_SETTLE_S)
            muted = _player(ctx, member).volume_muted
            assert muted is True, f"member volume_muted is {muted} after the group volume change"
            silent_from = changed + MUTE_SETTLE_S
            await _sleep_until(silent_from + MUTED_SILENT_S + LEVEL_SETTLE_S)
            sound = _sound(ctx, other, silent_from, silent_from + MUTED_SILENT_S)
            assert not sound, f"muted member sounds: {describe(sound, changed)}"

        with ctx.step("others_change", MUTED_MEMBER_ISSUES):
            level = _player(ctx, leader).volume_level
            ctx.record_property("leader_volume", level)
            assert level is not None and abs(level - MUTED_GROUP_VOLUME) <= VOLUME_TOLERANCE, (
                f"leader volume {level}, expected {MUTED_GROUP_VOLUME}"
            )
    finally:
        await _teardown(ctx, leader, member)


async def volume_set_audible(ctx: CheckContext, role: str) -> None:
    """Check a lower volume makes the capture quieter and full volume brings it back."""
    queue_id = ctx.player_id(role)
    tone = TWO_TRACKS[0]
    try:
        started = await play(ctx, queue_id, (tone,), VOLUME_SET_ISSUES)
        with ctx.step("quieter", VOLUME_SET_ISSUES):
            heard = await _heard(ctx, role, (tone,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)
            span_start = heard.t0 + LEVEL_LEAD_S
            loud = await _level(ctx, role, tone, span_start, span_start + LEVEL_SPAN_S)
            lowered = time.time()
            await ctx.client.players.volume_set(queue_id, LOWER_VOLUME)
            quiet = await _level_after(ctx, role, tone, lowered)
            ctx.record_property("full_level_db", round(loud, 1))
            ctx.record_property("lowered_level_db", round(quiet, 1))
            assert loud - quiet >= MIN_DROP_DB, (
                f"level {loud:.1f} -> {quiet:.1f} dBFS at volume {LOWER_VOLUME}"
            )

        with ctx.step("restored", VOLUME_SET_ISSUES):
            raised = time.time()
            await ctx.client.players.volume_set(queue_id, FULL_VOLUME)
            back = await _level_after(ctx, role, tone, raised)
            ctx.record_property("restored_level_db", round(back, 1))
            assert abs(back - loud) <= MAX_RESTORE_DIFF_DB, (
                f"level {back:.1f} dBFS back at full volume, was {loud:.1f}"
            )
    finally:
        await stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def group_volume_average(ctx: CheckContext, role: str) -> None:
    """Check the group volume reports the loudest member and moves every member when set."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    try:
        await _group(ctx, leader, member, GROUP_VOLUME_ISSUES)
        with ctx.step("reports_loudest", GROUP_VOLUME_ISSUES):
            await _set_volume(ctx, leader, GROUP_LEADER_VOLUME)
            await _set_volume(ctx, member, GROUP_MEMBER_VOLUME)
            await _until_player(
                ctx,
                leader,
                # the server reports the loudest powered member as the group volume
                f"group volume {GROUP_LEADER_VOLUME}",
                lambda p: _near(p.group_volume, GROUP_LEADER_VOLUME),
                GROUP_VOLUME_S,
            )

        with ctx.step("settable", GROUP_VOLUME_ISSUES):
            await ctx.client.players.group_volume(leader, GROUP_TARGET)

            def moved() -> bool:
                head, follower = ctx.client.players.get(leader), ctx.client.players.get(member)
                return (
                    head is not None
                    and follower is not None
                    and _near(head.group_volume, GROUP_TARGET)
                    and head.volume_level != GROUP_LEADER_VOLUME
                    and follower.volume_level != GROUP_MEMBER_VOLUME
                )

            try:
                await until(moved, GROUP_VOLUME_S, POLL_S)
            finally:
                head, follower = _player(ctx, leader), _player(ctx, member)
                ctx.record_property(
                    "group_volumes",
                    f"group {head.group_volume} leader {head.volume_level}"
                    f" member {follower.volume_level}",
                )
    finally:
        await _teardown(ctx, leader, member)


async def volume_after_pause(ctx: CheckContext, role: str) -> None:
    """Check the volume stays put through play, pause, resume and next."""
    queue_id = ctx.player_id(role)
    try:
        await _set_volume(ctx, queue_id, PAUSE_TEST_VOLUME)
        started = await play(ctx, queue_id, TWO_TRACKS, VOLUME_PAUSE_ISSUES)
        with ctx.step("volume_kept", VOLUME_PAUSE_ISSUES):
            await _volume_kept(ctx, queue_id, "play", started)
            await asyncio.sleep(PLAYED_BEFORE_PAUSE_S)
            for command, action in (
                ("pause", ctx.client.player_queues.pause),
                ("resume", ctx.client.player_queues.resume),
                ("next", ctx.client.player_queues.next),
            ):
                issued = time.time()
                await action(queue_id)
                await _volume_kept(ctx, queue_id, command, issued)
    finally:
        await stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def pause_holds(ctx: CheckContext, role: str) -> None:
    """Check a pause goes silent, holds for 20 s and resumes where it paused."""
    queue_id = ctx.player_id(role)
    try:
        await play(ctx, queue_id, (LONG_TONE,), PAUSE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_HOLD_S)

        paused = time.time()
        await ctx.client.player_queues.pause(queue_id)
        silent_from = paused + PAUSE_SILENT_WITHIN_S
        with ctx.step("pauses", PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                COMMAND_TIMEOUT_S,
                since=paused,
            )
            await _sleep_until(silent_from + LEVEL_READ_S)
            paused_at = _elapsed(ctx, queue_id)
            ctx.record_property("paused_elapsed_s", round(paused_at, 1))
            sound = _sound(ctx, role, silent_from, time.time())
            assert not sound, f"sound after pause: {describe(sound, paused)}"

        with ctx.step("holds", PAUSE_ISSUES):
            await _sleep_until(paused + HOLD_S)
            queue = ctx.client.player_queues.get(queue_id)
            state = queue.state if queue is not None else None
            assert state != PlaybackState.PLAYING, f"queue {state} {HOLD_S} s after pause"
            sound = _sound(ctx, role, silent_from, time.time())
            assert not sound, f"sound while paused: {describe(sound, paused)}"

        resumed = time.time()
        await ctx.client.player_queues.resume(queue_id)
        with ctx.step("resumes_in_place", PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 0),
                RESUME_TIMEOUT_S,
                since=resumed,
            )
            await _sleep_until(resumed + RESUME_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            ctx.record_property("resumed_elapsed_s", round(elapsed, 1))
            low, high = paused_at - MAX_RESUME_REWIND_S, paused_at + MAX_RESUME_ADVANCE_S
            assert low <= elapsed <= high, (
                f"elapsed {elapsed:.1f} s {RESUME_READ_S} s after resume, expected {low:.1f}"
                f" to {high:.1f} s"
            )
            await _heard(
                ctx, role, (LONG_TONE,), resumed, HEARD_MIN_S, _left(resumed + RESUME_TIMEOUT_S)
            )
    finally:
        await stop(ctx, queue_id)


async def disconnect_closes_stream(ctx: CheckContext, role: str) -> None:
    """Check a stopped player goes unavailable, its queue stops, and the player returns."""
    queue_id = ctx.player_id(role)
    try:
        await play(ctx, queue_id, (LONG_TONE,), DISCONNECT_ISSUES)
        stopped = time.time()
        await stop_role(role)
        try:
            with ctx.step("player_unavailable", DISCONNECT_ISSUES):
                await _until_player(
                    ctx, queue_id, "unavailable", lambda p: not p.available, DISCONNECT_TIMEOUT_S
                )
                ctx.record_property("unavailable_s", round(time.time() - stopped, 1))

            with ctx.step("queue_stops", DISCONNECT_ISSUES):
                await _until_queue_stopped(ctx, queue_id, _left(stopped + DISCONNECT_TIMEOUT_S))
            with ctx.step("client_queue_idle", CLIENT_QUEUE_ISSUES):
                await _until_client_queue_not_playing(ctx, queue_id, CLIENT_QUEUE_IDLE_S)
        finally:
            await start_role(role)

        with ctx.step("player_returns", DISCONNECT_ISSUES):
            await _until_player(ctx, queue_id, "available", lambda p: p.available, RETURN_TIMEOUT_S)
    finally:
        # a player that has not returned rejects the stop, which would mask the failed step
        with contextlib.suppress(PlayerUnavailableError):
            await stop(ctx, queue_id)


async def announce_ducks_and_resumes(ctx: CheckContext, role: str) -> None:
    """Check an announcement over music plays, then the music resumes at its volume and place."""
    queue_id = ctx.player_id(role)
    try:
        await _set_volume(ctx, queue_id, ANNOUNCE_MUSIC_VOLUME)
        await play(ctx, queue_id, TWO_TRACKS, ANNOUNCE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_ANNOUNCE_S)

        with ctx.step("clip_plays", ANNOUNCE_ISSUES):
            announced = time.time()
            music = ctx.capture(role).level(announced - LEVEL_SPAN_S, announced)
            await _announce(ctx, queue_id, CLIP, volume_level=ANNOUNCE_VOLUME)
            returned = time.time()
            clip = await _heard(ctx, role, (CLIP,), announced, CLIP_HEARD_S, CAPTURE_SETTLE_S)
            level = ctx.capture(role).level(clip.t0, clip.t1, CLIP)
            if music is not None:
                ctx.record_property("music_level_db", round(music, 1))
            if level is not None:
                ctx.record_property("clip_level_db", round(level, 1))
            assert music is not None and level is not None and level > music, (
                f"clip at {level} dBFS over music at {music} dBFS, announced at volume"
                f" {ANNOUNCE_VOLUME} over {ANNOUNCE_MUSIC_VOLUME}"
            )

        with ctx.step("music_resumes", ANNOUNCE_ISSUES):
            resumed = await _heard(
                ctx, role, A1_TONES, clip.t1, HEARD_MIN_S, _left(returned + MUSIC_RESUME_S)
            )

        with ctx.step("volume_restored", ANNOUNCE_ISSUES):
            await _until_player(
                ctx,
                queue_id,
                f"volume {ANNOUNCE_MUSIC_VOLUME}",
                lambda p: p.volume_level == ANNOUNCE_MUSIC_VOLUME,
                _left(returned + VOLUME_RESTORE_S),
            )

        with ctx.step("position_kept", ANNOUNCE_ISSUES):
            await _sleep_until(resumed.t0 + POSITION_READ_S)
            position = _elapsed(ctx, queue_id)
            ctx.record_property("resumed_position_s", round(position, 1))
            assert MIN_RESUMED_POSITION_S <= position <= MAX_RESUMED_POSITION_S, (
                f"position {position:.1f} s {POSITION_READ_S} s after the music resumed, expected"
                f" {MIN_RESUMED_POSITION_S} to {MAX_RESUMED_POSITION_S} s"
            )
    finally:
        await stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def announce_idle_only_clip(ctx: CheckContext, role: str) -> None:
    """Check an announcement on an idle player plays the clip and nothing after it."""
    queue_id = ctx.player_id(role)
    try:
        await stop(ctx, queue_id)
        with ctx.step("clip_plays", ANNOUNCE_IDLE_ISSUES):
            announced = time.time()
            await _announce(ctx, queue_id, CLIP)
            returned = time.time()
            clip = await _heard(ctx, role, (CLIP,), announced, CLIP_HEARD_S, CAPTURE_SETTLE_S)

        with ctx.step("nothing_else", ANNOUNCE_IDLE_ISSUES):
            await _sleep_until(returned + QUIET_AFTER_S)
            sound = _sound(ctx, role, clip.t1 + CLIP_TAIL_S, time.time())
            assert not sound, f"sound after the clip: {describe(sound, announced)}"
            assert not _is_playing(ctx, queue_id), "player plays after the announcement"
    finally:
        await stop(ctx, queue_id)


async def announce_short_and_back_to_back(ctx: CheckContext, role: str) -> None:
    """Check a 1.5 s clip plays whole, alone, twice in a row and after the chime."""
    queue_id = ctx.player_id(role)
    try:
        await stop(ctx, queue_id)
        with ctx.step("short_clip_full", ANNOUNCE_SHORT_ISSUES):
            announced = time.time()
            await _announce(ctx, queue_id, SHORT_CLIP)
            await _heard(ctx, role, (SHORT_CLIP,), announced, SHORT_CLIP_HEARD_S, CAPTURE_SETTLE_S)

        with ctx.step("back_to_back", ANNOUNCE_SHORT_ISSUES):
            announced = time.time()
            await _announce(ctx, queue_id, SHORT_CLIP)
            await _announce(ctx, queue_id, SHORT_CLIP)
            clips: list[Segment] = []

            def two_clips() -> bool:
                clips[:] = _clips(ctx, role, announced)
                return len(clips) >= 2

            try:
                await until(two_clips, CAPTURE_SETTLE_S, POLL_S)
            finally:
                ctx.record_property("back_to_back_clips", describe(clips, announced))

        with ctx.step("with_chime", ANNOUNCE_SHORT_ISSUES):
            announced = time.time()
            await _announce(ctx, queue_id, SHORT_CLIP, pre_announce=True)
            clip = await _heard(
                ctx, role, (SHORT_CLIP,), announced, SHORT_CLIP_HEARD_S, CAPTURE_SETTLE_S
            )
            chime = [
                seg
                for seg in ctx.capture(role).timeline()
                if seg.kind == "unknown" and seg.t0 >= announced and seg.t1 <= clip.t0
            ]
            chime_s = sum(seg.duration for seg in chime)
            assert chime_s >= CHIME_MIN_S, (
                f"{chime_s:.1f} s of unknown sound before the clip: {describe(chime, announced)}"
            )
    finally:
        await stop(ctx, queue_id)


async def _group(ctx: CheckContext, leader: str, member: str, issues: tuple[str, ...]) -> None:
    """Join `member` to `leader` in step `groups` and wait until it reports so."""
    with ctx.step("groups", issues):
        await ctx.client.players.group(member, leader)
        await _until_synced(ctx, member, leader, GROUP_TIMEOUT_S)
    await asyncio.sleep(GROUP_SETTLE_S)


async def _play_group(
    ctx: CheckContext, leader: str, tone_ids: tuple[str, ...], issues: tuple[str, ...]
) -> float:
    """Like `play`, but any playing index passes, as the server may resume instead of replace."""
    media = await tracks(ctx, tone_ids)
    started = time.time()
    with ctx.step("starts", issues):
        await ctx.client.player_queues.play_media(leader, media, QueueOption.REPLACE)
        await ctx.events.wait_for(
            EventType.QUEUE_UPDATED, queue_in(leader, "playing"), START_TIMEOUT_S, since=started
        )
    return started


async def _teardown(ctx: CheckContext, leader: str, member: str) -> None:
    """Stop the group, ungroup the member, put both back at full volume and let it settle."""
    await stop(ctx, leader)
    await ctx.client.players.ungroup(member)
    await _restore_volume(ctx, leader)
    await _restore_volume(ctx, member)
    await _until_ungrouped(ctx, leader, member)
    await asyncio.sleep(UNGROUP_SETTLE_S)


async def _remove_syncgroup(ctx: CheckContext, group_id: str, leader: str, member: str) -> None:
    """Stop and delete a syncgroup player, put its members back at full volume and release them."""
    try:
        await stop(ctx, group_id)
    finally:
        # the server keeps a static syncgroup in its config, so it would outlive this check
        await ctx.client.players.remove_group_player(group_id)
    await _restore_volume(ctx, leader)
    await _restore_volume(ctx, member)
    await _until_ungrouped(ctx, leader, member)
    await asyncio.sleep(UNGROUP_SETTLE_S)


async def _restart_member(ctx: CheckContext, role: str, issues: tuple[str, ...]) -> float:
    """Restart a role's container in steps `member_drops` and `member_returns`; return when back."""
    player_id = ctx.player_id(role)

    def unavailable(event: MassEvent) -> bool:
        return event.object_id == player_id and event.data.get("available") is False

    # from the event history: a Sendspin member is unavailable for only 0.2 s, shorter than a poll
    restarted = time.time()
    await restart_role(role)
    with ctx.step("member_drops", issues):
        drop = await ctx.events.wait_for(
            EventType.PLAYER_UPDATED, unavailable, DROP_TIMEOUT_S, since=restarted
        )
        dropped = next(r.time for r in ctx.events.events if r.event is drop)
        ctx.record_property("member_drop_s", round(dropped - restarted, 1))

    with ctx.step("member_returns", issues):
        await _until_player(ctx, player_id, "available", lambda p: p.available, RETURN_TIMEOUT_S)
        returned = time.time()
        ctx.record_property("member_return_s", round(returned - restarted, 1))
    return returned


async def _announce(
    ctx: CheckContext,
    player_id: str,
    tone_id: str,
    pre_announce: bool = False,
    volume_level: int | None = None,
) -> None:
    """Announce the lab radio's clip of a tone and return once the server says it finished."""
    url = f"{os.environ['LAB_RADIO_URL']}/tone/{tone_id}.mp3"
    async with asyncio.timeout(ANNOUNCE_TIMEOUT_S):
        await ctx.client.players.play_announcement(
            player_id, url, pre_announce=pre_announce, volume_level=volume_level
        )


async def _set_volume(ctx: CheckContext, player_id: str, level: int) -> None:
    """Set a player's volume and wait until it reports the level."""
    await ctx.client.players.volume_set(player_id, level)
    await _until_player(
        ctx, player_id, f"volume {level}", lambda p: p.volume_level == level, COMMAND_TIMEOUT_S
    )


async def _restore_volume(ctx: CheckContext, player_id: str) -> None:
    """Unmute a player and set it to full volume, as far as it has those controls."""
    player = ctx.client.players.get(player_id)
    if player is None or not player.available:
        return
    if PlayerFeature.VOLUME_MUTE in player.supported_features and player.volume_muted:
        await ctx.client.players.volume_mute(player_id, False)
    if PlayerFeature.VOLUME_SET in player.supported_features:
        await ctx.client.players.volume_set(player_id, FULL_VOLUME)


async def _volume_kept(ctx: CheckContext, player_id: str, command: str, issued: float) -> None:
    """Fail unless the player reports the test volume 1 s after `command`."""
    await _sleep_until(issued + VOLUME_SAMPLE_AFTER_S)
    level = _player(ctx, player_id).volume_level
    ctx.record_property(f"volume_after_{command}", level)
    assert level == PAUSE_TEST_VOLUME, f"volume {level} {VOLUME_SAMPLE_AFTER_S} s after {command}"


async def _level(ctx: CheckContext, role: str, tone_id: str, t0: float, t1: float) -> float:
    """Median capture level of a tone over [t0, t1], once captured."""
    await _sleep_until(t1 + LEVEL_SETTLE_S)
    level = ctx.capture(role).level(t0, t1, tone_id)
    assert level is not None, f"{tone_id} not heard on {role} over {t1 - t0:.1f} s"
    return level


async def _level_after(ctx: CheckContext, role: str, tone_id: str, changed: float) -> float:
    """Capture level of a tone over the last second of the 3 s after a volume change."""
    end = changed + VOLUME_EFFECT_S
    return await _level(ctx, role, tone_id, end - LEVEL_READ_S, end)


async def _heard(
    ctx: CheckContext,
    role: str,
    tone_ids: Collection[str],
    after: float,
    min_s: float,
    timeout: float,
) -> Segment:
    """Wait until the capture has `min_s` of one tone after `after`; return it from `after` on."""
    found: list[Segment] = []

    def heard() -> bool:
        found[:] = [
            run._replace(t0=max(run.t0, after))
            for run in tone_runs(ctx.capture(role, after).timeline(), tone_ids)
            if run.t1 - max(run.t0, after) >= min_s
        ][:1]
        return bool(found)

    heard.__name__ = f"{'/'.join(sorted(tone_ids))} heard {min_s:.1f} s on {role}"
    await until(heard, timeout, POLL_S)
    return found[0]


def _clips(ctx: CheckContext, role: str, after: float) -> list[Segment]:
    """Separate runs of the short clip starting after `after`."""
    runs: list[Segment] = []
    for seg in ctx.capture(role, after).timeline():
        if seg.kind != SHORT_CLIP or seg.t0 < after:
            continue
        if runs and seg.t0 - runs[-1].t1 < CLIP_MERGE_GAP_S:
            runs[-1] = runs[-1]._replace(t1=seg.t1)
        else:
            runs.append(seg)
    return [run for run in runs if run.duration >= TAIL_TONE_S]


def _sound(ctx: CheckContext, role: str, t0: float, t1: float) -> list[Segment]:
    """Segments other than silence overlapping [t0, t1]."""
    return [
        seg
        for seg in ctx.capture(role).timeline()
        if seg.kind != "silence" and seg.t1 > t0 and seg.t0 < t1
    ]


async def _until_player(
    ctx: CheckContext,
    player_id: str,
    what: str,
    check: Callable[[Player], bool],
    timeout: float,
) -> None:
    """Poll the client's view of a player until `check` holds."""

    def holds() -> bool:
        player = ctx.client.players.get(player_id)
        return player is not None and check(player)

    holds.__name__ = f"{player_id} {what}"
    await until(holds, timeout, POLL_S)


async def _until_queue_stopped(ctx: CheckContext, queue_id: str, timeout: float) -> None:
    """Poll the server until the queue is gone or no longer playing."""
    deadline = time.monotonic() + timeout
    while (
        queue := await ctx.client.send_command("player_queues/get", queue_id=queue_id)
    ) and queue["state"] == "playing":
        assert time.monotonic() < deadline, f"queue {queue_id} still playing after {timeout:.1f} s"
        await asyncio.sleep(POLL_S)


async def _until_client_queue_not_playing(ctx: CheckContext, queue_id: str, timeout: float) -> None:
    """Poll the client's cached queue until it is gone or no longer playing."""

    def holds() -> bool:
        queue = ctx.client.player_queues.get(queue_id)
        return queue is None or queue.state != PlaybackState.PLAYING

    holds.__name__ = f"{queue_id} client queue not playing"
    await until(holds, timeout, POLL_S)


async def _until_synced(ctx: CheckContext, member: str, leader: str, timeout: float) -> None:
    """Poll until the member reports it is synced to the leader."""
    await _until_player(
        ctx, member, f"synced to {leader}", lambda p: p.synced_to == leader, timeout
    )


async def _until_rejoined(ctx: CheckContext, group_id: str, member: str, timeout: float) -> None:
    """Poll until the member is synced to the syncgroup's current sync leader, which holds it."""

    def rejoined() -> bool:
        group, follower = ctx.client.players.get(group_id), ctx.client.players.get(member)
        lead = follower.synced_to if follower is not None else None
        head = ctx.client.players.get(lead) if lead is not None else None
        # a live syncgroup reports its sync leader's members, the leader included
        return (
            group is not None
            and head is not None
            and head.player_id in group.group_members
            and member in head.group_members
        )

    rejoined.__name__ = f"{member} synced to the sync leader of {group_id}"
    await until(rejoined, timeout, POLL_S)


async def _until_ungrouped(ctx: CheckContext, leader: str, member: str) -> None:
    """Poll until the member is unsynced and the leader no longer lists it."""

    def ungrouped() -> bool:
        follower, head = ctx.client.players.get(member), ctx.client.players.get(leader)
        return (follower is None or follower.synced_to is None) and (
            head is None or member not in head.group_members
        )

    ungrouped.__name__ = f"{member} ungrouped from {leader}"
    await until(ungrouped, UNGROUP_TIMEOUT_S, POLL_S)


def _player(ctx: CheckContext, player_id: str) -> Player:
    """Return the client's view of a player."""
    player = ctx.client.players.get(player_id)
    assert player is not None, f"player {player_id} unknown"
    return player


def _is_playing(ctx: CheckContext, player_id: str) -> bool:
    """Whether the player reports playing."""
    player = ctx.client.players.get(player_id)
    return player is not None and player.playback_state == PlaybackState.PLAYING


def _elapsed(ctx: CheckContext, queue_id: str) -> float:
    """Elapsed time of the queue's current item as the client sees it now."""
    queue = ctx.client.player_queues.get(queue_id)
    assert queue is not None, f"queue {queue_id} unknown"
    return float(queue.corrected_elapsed_time)


def _near(level: int | None, target: int) -> bool:
    """Whether a reported volume is within 1 of `target`."""
    return level is not None and abs(level - target) <= VOLUME_TOLERANCE


def _left(deadline: float) -> float:
    """Seconds until `deadline`, never negative."""
    return max(deadline - time.time(), 0.0)


async def _sleep_until(wall: float) -> None:
    """Sleep until the wall-clock time `wall`."""
    await asyncio.sleep(max(wall - time.time(), 0.0))


async def _when(condition: Callable[[], bool], timeout: float) -> float:
    """Wall time a polled condition first holds."""
    await until(condition, timeout, POLL_S)
    return time.time()
