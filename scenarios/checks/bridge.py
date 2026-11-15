"""Bridge checks: a squeezelite player plays in a Sendspin group through its Sendspin bridge."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState

from scenarios.checks.context import check
from scenarios.checks.players import (
    MAX_LEADER_GAP_S,
    SYNC_HEARD_S,
    SYNC_HEARD_TIMEOUT_S,
    _group,
    _heard,
    _left,
    _play_group,
    _player,
    _sleep_until,
    _sound,
    _teardown,
    _until_ungrouped,
    partner,
)
from scenarios.checks.queue import (
    ADVANCE_MARGIN_S,
    CAPTURE_SETTLE_S,
    HEARD_MIN_S,
    HEARD_TIMEOUT_S,
    describe,
    duration,
    play,
    stop,
    tone_runs,
)
from scenarios.helpers import silences

if TYPE_CHECKING:
    from music_assistant_models.player import Player

    from scenarios.checks.context import CheckContext

BRIDGE_ISSUES = ("server#7717",)

# the Sendspin player a squeezelite role is grouped with; it leads the group
SENDSPIN_ROLE = "sendspin-1"
SENDSPIN_DOMAIN = "sendspin"
NATIVE_OUTPUT = "native"
TWO_TRACKS = ("a1t1", "a1t2")
ALBUM = ("a2t1", "a2t2", "a2t3")
LONG_TONE = "a1t4"
SHORT_TONE = "a3t1"

# The virtual squeezelite counts a frame as played when the write before it returned, and the
# bridge puts that instant on the Sendspin timeline. The capture stamps the frame 18 pacer reads
# later (16 pages of the 64 KiB FIFO, the page the pacer holds and its stamp at the end of the
# wait: 384 ms), plus what sat in squeezelite's 4096 byte stdio buffer when the stream began (up
# to 21.3 ms, fixed for a stream). A Sendspin capture is stamped at the play-at time, so a
# bridged squeezelite role that is in sync lags it by 384.0 to 405.3 ms.
FIFO_DELAY_S = 0.384
FIFO_PHASE_S = 0.0214
# the analyzer reads up to 2 ms low; measured -1.1 to 0.0 ms once a start is pulled in, over 8
# starts (run 20261115-053044-fae1)
MAX_BRIDGE_ERROR_S = 0.006
# a start up to 10 ms off is pulled in at 0.5 ms/s, so the markers of its first seconds do not
# count; the first track change comes 20 s or more after a start
SYNC_SETTLE_S = 10.0
PLAYED_BEFORE_JOIN_S = 4.0
# a join is a cold start of the bridge stream; measured 2.5 s
JOIN_HEARD_S = 6.0
PLAYED_BEFORE_LEAVE_S = 4.0
# the FIFO plays out 0.4 s after the player stopped
MEMBER_SILENT_WITHIN_S = 1.5
LEADER_WATCH_S = 8.0
PLAYED_BEFORE_STOP_S = 5.0
# measured 0.42 s, of which 0.39 s is the FIFO
STOP_SILENT_WITHIN_S = 1.0
STOP_WATCH_S = 4.0
# one analysis window (85 ms) and what a resampler holds back at the end of a stream
MAX_END_CUT_S = 0.15
NATIVE_PLAYED_S = 8.0


@check
async def bridge_output_listed(ctx: CheckContext, role: str) -> None:
    """Check a squeezelite player lists a Sendspin output and the bridge adds no player."""
    player_id = ctx.player_id(role)
    with ctx.step("sendspin_output", BRIDGE_ISSUES):
        output = _sendspin_output(_player(ctx, player_id))
        assert output is not None, f"{role} has no Sendspin output next to its own"
        ctx.record_property("sendspin_output", output)

    with ctx.step("one_player", BRIDGE_ISSUES):
        named = [p.player_id for p in ctx.client.players if p.name == f"lab-{role}"]
        assert named == [player_id], f"players named lab-{role}: {named}"
        bridged = [p.name for p in ctx.client.players if p.name.endswith("(Squeezelite)")]
        assert not bridged, f"the bridge client shows as a player: {bridged}"


@check
async def mixed_group_plays_in_sync(ctx: CheckContext, role: str) -> None:
    """Check a squeezelite player plays in a Sendspin group, over Sendspin and in sync."""
    leader, member = ctx.player_id(SENDSPIN_ROLE), ctx.player_id(role)
    first, second = TWO_TRACKS
    try:
        await _group(ctx, leader, member, BRIDGE_ISSUES)
        started = await _play_group(ctx, leader, TWO_TRACKS, BRIDGE_ISSUES)
        with ctx.step("both_play", BRIDGE_ISSUES):
            for heard_on in (SENDSPIN_ROLE, role):
                await _heard(ctx, heard_on, (first,), started, SYNC_HEARD_S, SYNC_HEARD_TIMEOUT_S)

        with ctx.step("over_sendspin", BRIDGE_ISSUES):
            _assert_over_sendspin(ctx, member, leader)

        with ctx.step("both_advance", BRIDGE_ISSUES):
            deadline = started + duration(ctx, first) + ADVANCE_MARGIN_S
            for heard_on in (SENDSPIN_ROLE, role):
                await _heard(ctx, heard_on, (second,), started, HEARD_MIN_S, _left(deadline))

        with ctx.step("in_sync", BRIDGE_ISSUES):
            _assert_in_sync(ctx, role, started + SYNC_SETTLE_S)
    finally:
        await _teardown(ctx, leader, member)


@check
async def bridge_member_joins_and_leaves(ctx: CheckContext, role: str) -> None:
    """Check a squeezelite player joins a playing Sendspin player in sync and leaves it alone."""
    leader, member = ctx.player_id(SENDSPIN_ROLE), ctx.player_id(role)
    first, second, third = ALBUM
    try:
        started = await play(ctx, leader, ALBUM, BRIDGE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_JOIN_S)

        joined = time.time()
        await _group(ctx, leader, member, BRIDGE_ISSUES)
        with ctx.step("joins", BRIDGE_ISSUES):
            heard = await _heard(ctx, role, (first,), joined, HEARD_MIN_S, JOIN_HEARD_S)
            ctx.record_property("join_heard_s", round(heard.t0 - joined, 2))
            _assert_over_sendspin(ctx, member, leader)

        # the markers of the track change are the first both players have
        with ctx.step("in_sync", BRIDGE_ISSUES):
            deadline = started + duration(ctx, first) + ADVANCE_MARGIN_S
            for heard_on in (SENDSPIN_ROLE, role):
                await _heard(ctx, heard_on, (second,), joined, HEARD_MIN_S, _left(deadline))
            _assert_in_sync(ctx, role, joined + SYNC_SETTLE_S)
        await asyncio.sleep(PLAYED_BEFORE_LEAVE_S)

        left = time.time()
        await ctx.client.players.ungroup(member)
        with ctx.step("leaves", BRIDGE_ISSUES):
            await _until_ungrouped(ctx, leader, member)
            watched = left + LEADER_WATCH_S
            await _sleep_until(watched + CAPTURE_SETTLE_S)
            sound = _sound(ctx, role, left + MEMBER_SILENT_WITHIN_S, watched)
            assert not sound, f"member sounds after it left: {describe(sound, left)}"
            assert _player(ctx, member).playback_state != PlaybackState.PLAYING

        with ctx.step("leader_keeps_playing", BRIDGE_ISSUES):
            timeline = ctx.capture(SENDSPIN_ROLE).timeline()
            gaps = [
                seg for seg in silences(timeline, left, watched) if seg.duration >= MAX_LEADER_GAP_S
            ]
            assert not gaps, f"leader silent after the member left: {describe(gaps, left)}"
            assert tone_runs(timeline, (second, third)), "leader did not play on"
    finally:
        await _teardown(ctx, leader, member)


@check
async def mixed_group_stops_and_ends(ctx: CheckContext, role: str) -> None:
    """Check a stop silences a bridged squeezelite player at once and a queue plays to its end."""
    leader, member = ctx.player_id(SENDSPIN_ROLE), ctx.player_id(role)
    try:
        await _group(ctx, leader, member, BRIDGE_ISSUES)
        started = await _play_group(ctx, leader, (LONG_TONE,), BRIDGE_ISSUES)
        with ctx.step("both_play", BRIDGE_ISSUES):
            for heard_on in (SENDSPIN_ROLE, role):
                await _heard(ctx, heard_on, (LONG_TONE,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)
        await asyncio.sleep(PLAYED_BEFORE_STOP_S)

        stopped = time.time()
        await ctx.client.player_queues.stop(leader)
        with ctx.step("stops_at_once", BRIDGE_ISSUES):
            watched = stopped + STOP_WATCH_S
            await _sleep_until(watched + CAPTURE_SETTLE_S)
            for heard_on in (SENDSPIN_ROLE, role):
                sound = _sound(ctx, heard_on, stopped + STOP_SILENT_WITHIN_S, watched)
                assert not sound, f"{heard_on} sounds after the stop: {describe(sound, stopped)}"
            last = [seg.t1 for seg in ctx.capture(role).timeline() if seg.kind == LONG_TONE]
            ctx.record_property("member_stop_s", round(max(last) - stopped, 2))

        started = await _play_group(ctx, leader, (SHORT_TONE,), BRIDGE_ISSUES)
        with ctx.step("plays_to_the_end", BRIDGE_ISSUES):
            ends = {}
            for heard_on in (SENDSPIN_ROLE, role):
                await _heard(ctx, heard_on, (SHORT_TONE,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)
            await _sleep_until(started + duration(ctx, SHORT_TONE) + HEARD_TIMEOUT_S)
            for heard_on in (SENDSPIN_ROLE, role):
                timeline = [s for s in ctx.capture(heard_on).timeline() if s.t1 > started]
                ends[heard_on] = tone_runs(timeline, (SHORT_TONE,))[-1].t1
            cut = ends[SENDSPIN_ROLE] + FIFO_DELAY_S - ends[role]
            ctx.record_property("member_end_cut_s", round(cut, 3))
            assert cut <= MAX_END_CUT_S, f"{role} ends {cut:.3f} s before the track does"
            assert _player(ctx, member).playback_state != PlaybackState.PLAYING
    finally:
        await _teardown(ctx, leader, member)


@check
async def squeezelite_group_stays_native(ctx: CheckContext, role: str) -> None:
    """Check a group of squeezelite players and a player on its own keep their own protocol."""
    other = partner(role)
    leader, member = ctx.player_id(role), ctx.player_id(other)
    first = TWO_TRACKS[0]
    try:
        await _group(ctx, leader, member, BRIDGE_ISSUES)
        started = await _play_group(ctx, leader, TWO_TRACKS, BRIDGE_ISSUES)
        with ctx.step("pair_plays_native", BRIDGE_ISSUES):
            for heard_on in (role, other):
                await _heard(
                    ctx, heard_on, (first,), started, NATIVE_PLAYED_S, SYNC_HEARD_TIMEOUT_S
                )
            output = _player(ctx, leader).active_output_protocol
            assert output == NATIVE_OUTPUT, f"leader plays on {output}"
            # a sync member rides the stream of its leader and has no output of its own
            output = _player(ctx, member).active_output_protocol
            assert output is None, f"member plays on {output}"
        await stop(ctx, leader)
        await ctx.client.players.ungroup(member)
        await _until_ungrouped(ctx, leader, member)

        started = await play(ctx, leader, (first,), BRIDGE_ISSUES)
        with ctx.step("solo_plays_native", BRIDGE_ISSUES):
            await _heard(ctx, role, (first,), started, HEARD_MIN_S, HEARD_TIMEOUT_S)
            output = _player(ctx, leader).active_output_protocol
            assert output == NATIVE_OUTPUT, f"active output {output}"
    finally:
        await _teardown(ctx, leader, member)


def _sendspin_output(player: Player) -> str | None:
    """Return the id of the Sendspin output a player has next to its own protocol."""
    return next(
        (
            output.output_protocol_id
            for output in player.output_protocols
            if output.protocol_domain == SENDSPIN_DOMAIN and not output.is_native
        ),
        None,
    )


def _assert_over_sendspin(ctx: CheckContext, member: str, leader: str) -> None:
    """Fail unless the member plays along with the leader on its Sendspin output."""
    player = _player(ctx, member)
    assert player.synced_to == leader, f"member synced to {player.synced_to}"
    assert player.playback_state == PlaybackState.PLAYING, f"member {player.playback_state}"
    output = player.active_output_protocol
    assert output is not None and output == _sendspin_output(player), f"member plays on {output}"
    media = player.current_media
    uri = media.uri if media is not None else None
    assert uri is not None and "/slimproto/" not in uri, f"member shows {uri}"


def _assert_in_sync(ctx: CheckContext, role: str, since: float) -> None:
    """Fail unless the role lags the Sendspin player by the delay of the lab's FIFO from `since`."""
    offset = ctx.capture(SENDSPIN_ROLE, since).sync_offset(ctx.capture(role, since))
    assert offset is not None, f"no offset the markers on {SENDSPIN_ROLE} and {role} agree on"
    ctx.record_property("sync_offset_s", round(offset, 4))
    error = max(FIFO_DELAY_S - offset, offset - FIFO_DELAY_S - FIFO_PHASE_S, 0.0)
    ctx.record_property("sync_error_s", round(error, 4))
    assert error <= MAX_BRIDGE_ERROR_S, (
        f"{role} lags {SENDSPIN_ROLE} by {offset:.4f} s, {error:.4f} s outside the"
        f" {FIFO_DELAY_S} to {FIFO_DELAY_S + FIFO_PHASE_S:.4f} s of the lab's FIFO"
    )
