"""Bench checks for real devices: queue state, position and elapsed time, no audio capture."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from labctl.devices import DEVICE_VOLUME, Device, matches
from scenarios.checks.containers import restart_role
from scenarios.checks.library import RESTART_TIMEOUT_S, SERVER_SERVICE
from scenarios.checks.queue import (
    POLL_S,
    duration,
    play_media,
    queue_in,
    queue_left_playing,
    stop,
    title,
    tracks,
)
from scenarios.checks.streams import ICY_PATH, radio_url
from scenarios.helpers import queue_field, queue_playing, until

if TYPE_CHECKING:
    from music_assistant_models.enums import PlayerFeature
    from music_assistant_models.player import Player

    from scenarios.checks.context import CheckContext

ALBUM_ISSUES = ("#6666", "#6562", "#6860")
PAUSE_ISSUES = ("#6614", "#5276")
SEEK_ISSUES = ("#6798", "#6181")
END_POSITION_ISSUES = ("#6839",)
NEXT_ISSUES = ("server#7147",)
GROUP_SKIP_ISSUES = ("server#6611",)
VOLUME_ISSUES = ("#5018", "#6664", "#5870")
MUTE_ISSUES = ("#5098", "#6664")
VOLUME_KEPT_ISSUES = ("#6633", "#5018")
LONG_PAUSE_ISSUES = ("#6672", "#6614", "#5276")
STOP_PLAY_ISSUES = ("#6839", "#6614")
RADIO_ISSUES = ("#6504", "#5103")
GROUP_STATE_ISSUES = ("#6779", "server#6611")

ALBUM = ("a3t1", "a3t2", "a3t3")
LONG_TONE = "a1t4"
# real devices buffer and start slower than the virtual players
START_TIMEOUT_S = 20.0
ADVANCE_MARGIN_S = 20.0
COMMAND_TIMEOUT_S = 10.0

PLAYED_BEFORE_PAUSE_S = 5.0
HOLD_S = 15.0
RESUME_READ_S = 2.0
MAX_RESUME_REWIND_S = 1.0
MAX_RESUME_ADVANCE_S = 6.0

PLAYED_BEFORE_SEEK_S = 5.0
SEEK_TARGET_S = 40
MIN_SEEK_REPORTED_S = 39.0
MAX_SEEK_REPORTED_S = 48.0
SEEK_REPORTED_TIMEOUT_S = 8.0
# a1t4 is 60 s, so the track ends 20 s after a seek to 40 s
MIN_SEEK_END_S = 12.0
MAX_SEEK_END_S = 35.0

# late time updates after the end are what #6839 looked like
POSITION_SETTLE_S = 3.0
FRESH_READ_S = 2.0
MAX_FRESH_ELAPSED_S = 5.0

PLAYED_BEFORE_NEXT_S = 3.0
NO_SECOND_SKIP_S = 5.0
GROUP_TIMEOUT_S = 10.0

# devices bootstrap at DEVICE_VOLUME (1); the volume checks stay about as quiet
DEVICE_TEST_VOLUME = 3
VOLUME_SAMPLE_AFTER_S = 1.0
VOLUME_COMMAND_GAP_S = 2.0
LONG_HOLD_S = 60.0
# player_queues controller `_watch_pause` stops a queue paused for 30 s
AUTO_STOP_S = 30.0
PLAYED_BEFORE_STOP_S = 5.0
STOP_IDLE_TIMEOUT_S = 10.0
RADIO_KEEPS_PLAYING_S = 10.0
GROUP_PLAY_TIMEOUT_S = 15.0
REDISCOVER_TIMEOUT_S = 90.0


async def bench_album(ctx: CheckContext, device: str) -> None:
    """Check the 10 s album advances through all three tracks and ends without overrunning."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    reported_names: list[str | None] = []
    try:
        started = await _play(ctx, queue_id, ALBUM, ALBUM_ISSUES)
        with ctx.step("advances", ALBUM_ISSUES):
            for index in range(1, len(ALBUM)):
                timeout = duration(ctx, ALBUM[index - 1]) + ADVANCE_MARGIN_S
                await _wait_playing(ctx, queue_id, index, timeout, since=started)
                if index == 1:
                    reported_names.append(_current_name(ctx, queue_id))
            last_started = time.time()

        with ctx.step("ends", ALBUM_ISSUES):
            last_s = duration(ctx, ALBUM[-1])
            event = await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                last_s + ADVANCE_MARGIN_S,
                since=last_started,
            )
            ctx.record_property("album_end_state", queue_field(event, "state"))
            elapsed = float(queue_field(event, "elapsed_time"))
            ctx.record_property("album_end_elapsed_s", round(elapsed, 1))
            assert elapsed <= last_s, f"elapsed {elapsed:.1f} s at the end of a {last_s} s track"

        with ctx.step("now_playing_matches", ALBUM_ISSUES):
            expected = title(ctx, ALBUM[1])
            assert reported_names == [expected], (
                f"now playing at index 1 was {reported_names}, expected {expected!r}"
            )
    finally:
        await _stop(ctx, queue_id)


async def bench_pause_resume(ctx: CheckContext, device: str) -> None:
    """Check a pause holds its state for 15 s and resume plays on from the paused position."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    try:
        await _play(ctx, queue_id, (LONG_TONE,), PAUSE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_PAUSE_S)

        paused = time.time()
        await ctx.client.player_queues.pause(queue_id)
        with ctx.step("pauses", PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                COMMAND_TIMEOUT_S,
                since=paused,
            )
            paused_at = _elapsed(ctx, queue_id)
            ctx.record_property("paused_elapsed_s", round(paused_at, 1))

        with ctx.step("holds", PAUSE_ISSUES):
            await _sleep_until(paused + HOLD_S)
            state = _state(ctx, queue_id)
            assert state != "playing", f"queue {state} {HOLD_S} s after pause"

        resumed = time.time()
        await ctx.client.player_queues.resume(queue_id)
        with ctx.step("resumes_in_place", PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 0),
                COMMAND_TIMEOUT_S,
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
    finally:
        await _stop(ctx, queue_id)


async def bench_long_pause(ctx: CheckContext, device: str) -> None:
    """Check the server stops a pause after 30 s and resume plays on from the paused position."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    try:
        await _play(ctx, queue_id, (LONG_TONE,), LONG_PAUSE_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_PAUSE_S)

        paused = time.time()
        await ctx.client.player_queues.pause(queue_id)
        with ctx.step("pauses", LONG_PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                COMMAND_TIMEOUT_S,
                since=paused,
            )
            paused_at = _elapsed(ctx, queue_id)
            ctx.record_property("paused_elapsed_s", round(paused_at, 1))

        # a stop-style pause is idle at once, a held pause is stopped by the server at 30 s
        with ctx.step("auto_stops", LONG_PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_in(queue_id, "idle"),
                AUTO_STOP_S + COMMAND_TIMEOUT_S,
                since=paused,
            )
            ctx.record_property("idle_after_s", round(time.time() - paused, 1))

        with ctx.step("holds", LONG_PAUSE_ISSUES):
            await _sleep_until(paused + LONG_HOLD_S)
            state = _state(ctx, queue_id)
            assert state == "idle", f"queue {state} {LONG_HOLD_S} s after pause"

        resumed = time.time()
        await ctx.client.player_queues.resume(queue_id)
        with ctx.step("resumes_in_place", LONG_PAUSE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 0),
                COMMAND_TIMEOUT_S,
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
    finally:
        await _stop(ctx, queue_id)


async def bench_seek(ctx: CheckContext, device: str) -> None:
    """Check a seek to 40 s reports the new position and the track then plays to its end."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    try:
        await _play(ctx, queue_id, (LONG_TONE,), SEEK_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_SEEK_S)
        seeked = time.time()
        await ctx.client.player_queues.seek(queue_id, SEEK_TARGET_S)

        with ctx.step("position_reported", SEEK_ISSUES):
            reported: list[float] = []

            def at_target() -> bool:
                reported[:] = [_elapsed(ctx, queue_id)]
                return MIN_SEEK_REPORTED_S <= reported[0] <= MAX_SEEK_REPORTED_S

            try:
                await until(at_target, SEEK_REPORTED_TIMEOUT_S, POLL_S)
            finally:
                if reported:
                    ctx.record_property("seek_reported_s", round(reported[0], 1))

        with ctx.step("no_restart", SEEK_ISSUES):
            # from here on, so a stop and start inside the seek itself does not count as the end
            listening = time.time()
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                seeked + MAX_SEEK_END_S - listening,
                since=listening,
            )
            seek_end = time.time() - seeked
            ctx.record_property("seek_end_s", round(seek_end, 1))
            assert seek_end >= MIN_SEEK_END_S, f"track ended {seek_end:.1f} s after the seek"
    finally:
        await _stop(ctx, queue_id)


async def bench_stop_position(ctx: CheckContext, device: str) -> None:
    """Check a track that played to its end never reports a position past it and replays from 0."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    tone = ALBUM[0]
    track_s = duration(ctx, tone)
    try:
        started = await _play(ctx, queue_id, (tone,), END_POSITION_ISSUES)
        playing = time.time()
        with ctx.step("position_not_past_end", END_POSITION_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                track_s + ADVANCE_MARGIN_S,
                since=playing,
            )
            await asyncio.sleep(POSITION_SETTLE_S)
            elapsed, resume_pos = _reported_positions(ctx, queue_id, started, title(ctx, tone))
            ctx.record_property("max_elapsed_s", round(elapsed, 1))
            ctx.record_property("max_resume_pos_s", resume_pos)
            assert elapsed <= track_s, f"elapsed reached {elapsed:.1f} s of a {track_s} s track"
            assert 0 <= resume_pos <= track_s, f"resume position {resume_pos} s"

        await _play(ctx, queue_id, (tone,), END_POSITION_ISSUES)
        replaying = time.time()
        with ctx.step("next_play_starts_at_zero", END_POSITION_ISSUES):
            await _sleep_until(replaying + FRESH_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            ctx.record_property("replay_elapsed_s", round(elapsed, 1))
            assert elapsed < MAX_FRESH_ELAPSED_S, f"replay reports {elapsed:.1f} s elapsed"
    finally:
        await _stop(ctx, queue_id)


async def bench_next_skips_one(ctx: CheckContext, device: str) -> None:
    """Check next moves exactly one track ahead."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    try:
        await _play(ctx, queue_id, ALBUM, NEXT_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_NEXT_S)

        skipped = time.time()
        await ctx.client.player_queues.next(queue_id)
        with ctx.step("skips_one", NEXT_ISSUES):
            await _wait_playing(ctx, queue_id, 1, COMMAND_TIMEOUT_S, since=skipped)
            await asyncio.sleep(NO_SECOND_SKIP_S)
            third = queue_playing(queue_id, 2)
            skipped_twice = [
                recorded.time - skipped
                for recorded in ctx.events.events
                if recorded.time >= skipped
                and recorded.event.event == EventType.QUEUE_UPDATED
                and third(recorded.event)
            ]
            assert not skipped_twice, f"queue reached index 2 {skipped_twice[0]:.1f} s after next"
    finally:
        await _stop(ctx, queue_id)


async def bench_group_skip(ctx: CheckContext, device: str, second: str) -> None:
    """Check next on a group of two devices skips one track on both and keeps them grouped."""
    leader, member = ctx.player_id(device), ctx.player_id(second)
    try:
        with ctx.step("groups", GROUP_SKIP_ISSUES):
            await ctx.client.players.group(member, leader)
            await _until_synced(ctx, member, leader, GROUP_TIMEOUT_S)

        await _play(ctx, leader, ALBUM, GROUP_SKIP_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_NEXT_S)

        skipped = time.time()
        await ctx.client.player_queues.next(leader)
        with ctx.step("both_skip", GROUP_SKIP_ISSUES):
            await _wait_playing(ctx, leader, 1, COMMAND_TIMEOUT_S, since=skipped)
            await until(_all_playing(ctx, leader, member), COMMAND_TIMEOUT_S, POLL_S)

        with ctx.step("group_intact", GROUP_SKIP_ISSUES):
            follower = ctx.client.players.get(member)
            synced_to = follower.synced_to if follower is not None else None
            assert synced_to == leader, f"member synced to {synced_to}"
    finally:
        await _stop(ctx, leader)
        await ctx.client.players.ungroup(member)


async def bench_volume_reported(ctx: CheckContext, device: str) -> None:
    """Check the device reports a volume set while it plays, up to 3 and back to 1."""
    from music_assistant_models.enums import PlayerFeature

    queue_id = ctx.player_id(device)
    if not _recorded_support(ctx, queue_id, "volume_checked", PlayerFeature.VOLUME_SET):
        return
    try:
        await _play(ctx, queue_id, (LONG_TONE,), VOLUME_ISSUES)
        with ctx.step("reports_test_volume", VOLUME_ISSUES):
            await _set_volume(ctx, queue_id, DEVICE_TEST_VOLUME)
        with ctx.step("reports_device_volume", VOLUME_ISSUES):
            await _set_volume(ctx, queue_id, DEVICE_VOLUME)
    finally:
        await _stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def bench_mute(ctx: CheckContext, device: str) -> None:
    """Check mute and unmute are reported while the device plays and unmute keeps the level."""
    from music_assistant_models.enums import PlayerFeature

    queue_id = ctx.player_id(device)
    features = (PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE)
    if not _recorded_support(ctx, queue_id, "mute_checked", *features):
        return
    try:
        await _play(ctx, queue_id, (LONG_TONE,), MUTE_ISSUES)
        with ctx.step("mutes", MUTE_ISSUES):
            await _set_volume(ctx, queue_id, DEVICE_TEST_VOLUME)
            await ctx.client.players.volume_mute(queue_id, True)
            await _until_player(
                ctx, queue_id, "muted", lambda p: p.volume_muted is True, COMMAND_TIMEOUT_S
            )

        with ctx.step("unmute_keeps_level", MUTE_ISSUES):
            await ctx.client.players.volume_mute(queue_id, False)
            await _until_player(
                ctx,
                queue_id,
                f"unmuted at volume {DEVICE_TEST_VOLUME}",
                lambda p: p.volume_muted is False and p.volume_level == DEVICE_TEST_VOLUME,
                COMMAND_TIMEOUT_S,
            )
    finally:
        await _stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def bench_volume_kept(ctx: CheckContext, device: str) -> None:
    """Check the device keeps volume 3 through play, pause, resume and next."""
    from music_assistant_models.enums import PlayerFeature

    queue_id = ctx.player_id(device)
    if not _recorded_support(ctx, queue_id, "volume_checked", PlayerFeature.VOLUME_SET):
        return
    try:
        with ctx.step("sets_volume", VOLUME_KEPT_ISSUES):
            await _set_volume(ctx, queue_id, DEVICE_TEST_VOLUME)
        await _play(ctx, queue_id, ALBUM, VOLUME_KEPT_ISSUES)
        with ctx.step("volume_kept", VOLUME_KEPT_ISSUES):
            await _volume_kept(ctx, queue_id, "play", time.time())
            for command, action in (
                ("pause", ctx.client.player_queues.pause),
                ("resume", ctx.client.player_queues.resume),
                ("next", ctx.client.player_queues.next),
            ):
                await asyncio.sleep(VOLUME_COMMAND_GAP_S)
                issued = time.time()
                await action(queue_id)
                await _volume_kept(ctx, queue_id, command, issued)
    finally:
        await _stop(ctx, queue_id)
        await _restore_volume(ctx, queue_id)


async def bench_stop_then_play(ctx: CheckContext, device: str) -> None:
    """Check stop mid-track goes idle within 10 s and the next play starts from the beginning."""
    from music_assistant_models.enums import EventType

    queue_id = ctx.player_id(device)
    try:
        await _play(ctx, queue_id, (LONG_TONE,), STOP_PLAY_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_STOP_S)

        stopped = time.time()
        await ctx.client.player_queues.stop(queue_id)
        with ctx.step("stops", STOP_PLAY_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_in(queue_id, "idle"),
                STOP_IDLE_TIMEOUT_S,
                since=stopped,
            )
            ctx.record_property("stopped_s", round(time.time() - stopped, 2))

        await _play(ctx, queue_id, (LONG_TONE,), STOP_PLAY_ISSUES)
        replaying = time.time()
        with ctx.step("replays_from_start", STOP_PLAY_ISSUES):
            await _sleep_until(replaying + FRESH_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            ctx.record_property("replay_elapsed_s", round(elapsed, 1))
            assert elapsed < MAX_FRESH_ELAPSED_S, f"replay reports {elapsed:.1f} s elapsed"
    finally:
        await _stop(ctx, queue_id)


async def bench_radio_starts(ctx: CheckContext, device: str) -> None:
    """Check the lab radio's ICY stream starts playing within 15 s and keeps playing 10 s."""
    queue_id = ctx.player_id(device)
    try:
        started = await play_media(ctx, queue_id, radio_url(ICY_PATH), RADIO_ISSUES)
        playing = time.time()
        ctx.record_property("radio_playing_s", round(playing - started, 2))
        with ctx.step("keeps_playing", RADIO_ISSUES):
            await _sleep_until(playing + RADIO_KEEPS_PLAYING_S)
            state = _state(ctx, queue_id)
            assert state == "playing", f"queue {state} {RADIO_KEEPS_PLAYING_S} s after it started"
    finally:
        await _stop(ctx, queue_id)


async def bench_group_state(ctx: CheckContext, device: str, second: str) -> None:
    """Check a group of two devices plays on both, survives next and ungroup leaves the leader."""
    from music_assistant_models.enums import PlaybackState

    leader = ctx.player_id(device)
    with ctx.step("second_available", GROUP_STATE_ISSUES):
        assert second in ctx.players, f"{second} was not available when the run resolved players"
    member = ctx.player_id(second)
    try:
        with ctx.step("groups", GROUP_STATE_ISSUES):
            await ctx.client.players.group(member, leader)
            await _until_synced(ctx, member, leader, GROUP_TIMEOUT_S)

        # the 60 s tone first, so the queue cannot advance by itself before next
        await _play(ctx, leader, (LONG_TONE, ALBUM[0]), GROUP_STATE_ISSUES)
        with ctx.step("both_play", GROUP_STATE_ISSUES):
            await until(_all_playing(ctx, leader, member), GROUP_PLAY_TIMEOUT_S, POLL_S)

        skipped = time.time()
        await ctx.client.player_queues.next(leader)
        with ctx.step("skip_keeps_group", GROUP_STATE_ISSUES):
            await _wait_playing(ctx, leader, 1, COMMAND_TIMEOUT_S, since=skipped)
            await until(_all_playing(ctx, leader, member), COMMAND_TIMEOUT_S, POLL_S)
            synced_to = _player(ctx, member).synced_to
            assert synced_to == leader, f"member synced to {synced_to}"

        await ctx.client.players.ungroup(member)
        with ctx.step("ungroup_leaves_leader", GROUP_STATE_ISSUES):
            await _until_player(
                ctx,
                member,
                "ungrouped and idle",
                lambda p: p.synced_to is None and p.playback_state == PlaybackState.IDLE,
                COMMAND_TIMEOUT_S,
            )
            state = _player(ctx, leader).playback_state
            assert state == PlaybackState.PLAYING, f"leader {state} once the member left"
    finally:
        await _stop(ctx, leader)
        await ctx.client.players.ungroup(member)


async def bench_restart_rediscovers(ctx: CheckContext, devices: dict[str, Device]) -> None:
    """Check every device is available again after a server restart and the first one plays."""
    with ctx.step("all_available"):
        missing = sorted(device_id for device_id in devices if device_id not in ctx.players)
        assert not missing, f"not available before the restart: {' '.join(missing)}"
    before = {device_id: ctx.player_id(device_id) for device_id in devices}
    async with contextlib.AsyncExitStack() as stack:
        with ctx.step("restarts"):
            restarted = time.time()
            await restart_role(SERVER_SERVICE)
            client = await stack.enter_async_context(ctx.reconnect(RESTART_TIMEOUT_S))
            ctx.record_property("restart_s", round(time.time() - restarted, 1))

        with ctx.step("rediscovered"):
            players = await _until_rediscovered(
                dataclasses.replace(ctx, client=client), devices, restarted
            )
            changed = sorted(d for d, player_id in players.items() if before[d] != player_id)
            ctx.record_property("player_ids_changed", " ".join(changed))

        restarted_ctx = dataclasses.replace(ctx, client=client, players=players)
        queue_id = players[min(devices)]
        try:
            await _play(restarted_ctx, queue_id, (LONG_TONE,), (), step="plays_after_restart")
        finally:
            await _stop(restarted_ctx, queue_id)


async def _play(
    ctx: CheckContext,
    queue_id: str,
    tone_ids: tuple[str, ...],
    issues: tuple[str, ...],
    step: str = "starts",
) -> float:
    """Replace the queue with the tones and wait in step `step` until it plays index 0."""
    from music_assistant_models.enums import QueueOption

    media = await tracks(ctx, tone_ids)
    started = time.time()
    with ctx.step(step, issues):
        await ctx.client.player_queues.play_media(queue_id, media, QueueOption.REPLACE)
        await _wait_playing(ctx, queue_id, 0, START_TIMEOUT_S, since=started)
    return started


async def _wait_playing(
    ctx: CheckContext, queue_id: str, index: int, timeout: float, since: float | None = None
) -> None:
    """Wait for a QUEUE_UPDATED event of the queue playing `index`."""
    from music_assistant_models.enums import EventType

    await ctx.events.wait_for(
        EventType.QUEUE_UPDATED, queue_playing(queue_id, index), timeout, since
    )


async def _stop(ctx: CheckContext, queue_id: str) -> None:
    """Stop the queue and let the device and the server settle before the next check."""
    await stop(ctx, queue_id, COMMAND_TIMEOUT_S)


def _recorded_support(
    ctx: CheckContext, player_id: str, name: str, *features: PlayerFeature
) -> bool:
    """Record under `name` and return whether the player supports all of `features`."""
    supported = set(features) <= _player(ctx, player_id).supported_features
    ctx.record_property(name, supported)
    return supported


async def _set_volume(ctx: CheckContext, player_id: str, level: int) -> None:
    """Set a player's volume and wait until it reports the level."""
    await ctx.client.players.volume_set(player_id, level)
    await _until_player(
        ctx, player_id, f"volume {level}", lambda p: p.volume_level == level, COMMAND_TIMEOUT_S
    )


async def _restore_volume(ctx: CheckContext, player_id: str) -> None:
    """Unmute the device and set it back to the bootstrap volume, as far as it has the controls."""
    from music_assistant_models.enums import PlayerFeature

    player = _player(ctx, player_id)
    if PlayerFeature.VOLUME_MUTE in player.supported_features:
        await ctx.client.players.volume_mute(player_id, False)
    if PlayerFeature.VOLUME_SET in player.supported_features:
        await ctx.client.players.volume_set(player_id, DEVICE_VOLUME)


async def _volume_kept(ctx: CheckContext, player_id: str, command: str, issued: float) -> None:
    """Fail unless the player reports the test volume 1 s after `command`."""
    await _sleep_until(issued + VOLUME_SAMPLE_AFTER_S)
    level = _player(ctx, player_id).volume_level
    ctx.record_property(f"volume_after_{command}", level)
    assert level == DEVICE_TEST_VOLUME, f"volume {level} {VOLUME_SAMPLE_AFTER_S} s after {command}"


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


async def _until_rediscovered(
    ctx: CheckContext, devices: dict[str, Device], since: float
) -> dict[str, str]:
    """Poll until every device has an available player; records when each one showed up."""
    found: dict[str, str] = {}

    def all_found() -> bool:
        for device_id, device in devices.items():
            player_id = _device_player(ctx, device)
            if device_id not in found and player_id is not None:
                found[device_id] = player_id
                ctx.record_property(f"rediscovered_s_{device_id}", round(time.time() - since, 1))
        return len(found) == len(devices)

    all_found.__name__ = "every device available"
    await until(all_found, REDISCOVER_TIMEOUT_S, POLL_S)
    return found


def _device_player(ctx: CheckContext, device: Device) -> str | None:
    """Player id of the device's available player, if the server has one."""
    return next(
        (
            p.player_id
            for p in ctx.client.players
            if p.available
            and matches(device, p.name, p.provider, [o.protocol_domain for o in p.output_protocols])
        ),
        None,
    )


def _player(ctx: CheckContext, player_id: str) -> Player:
    """Return the client's view of a player."""
    player = ctx.client.players.get(player_id)
    assert player is not None, f"player {player_id} unknown"
    return player


def _all_playing(ctx: CheckContext, *player_ids: str) -> Callable[[], bool]:
    """Condition that every one of the players reports playing, for `until`."""
    from music_assistant_models.enums import PlaybackState

    def playing() -> bool:
        return all(
            (player := ctx.client.players.get(player_id)) is not None
            and player.playback_state == PlaybackState.PLAYING
            for player_id in player_ids
        )

    playing.__name__ = f"{' and '.join(player_ids)} playing"
    return playing


async def _until_synced(ctx: CheckContext, member: str, leader: str, timeout: float) -> None:
    """Poll until the member reports it is synced to the leader."""

    def synced() -> bool:
        player = ctx.client.players.get(member)
        return player is not None and player.synced_to == leader

    synced.__name__ = f"{member} synced to {leader}"
    await until(synced, timeout, POLL_S)


def _current_name(ctx: CheckContext, queue_id: str) -> str | None:
    """Name of the media item the queue reports as now playing."""
    queue = ctx.client.player_queues.get(queue_id)
    item = queue.current_item if queue is not None else None
    return item.media_item.name if item is not None and item.media_item is not None else None


def _state(ctx: CheckContext, queue_id: str) -> str | None:
    """State of the queue as the client sees it now."""
    queue = ctx.client.player_queues.get(queue_id)
    return queue.state.value if queue is not None else None


def _elapsed(ctx: CheckContext, queue_id: str) -> float:
    """Elapsed time of the queue's current item as the client sees it now."""
    queue = ctx.client.player_queues.get(queue_id)
    assert queue is not None, f"queue {queue_id} unknown"
    return float(queue.corrected_elapsed_time)


def _reported_positions(
    ctx: CheckContext, queue_id: str, since: float, name: str
) -> tuple[float, int]:
    """Highest elapsed time and resume position the queue reported for item `name` since `since`."""
    from music_assistant_models.enums import EventType

    elapsed: list[float] = []
    resume: list[int] = []
    # play_media first republishes the previous item at its final position
    ours = False
    for recorded in ctx.events.events:
        event = recorded.event
        if recorded.time < since or event.object_id != queue_id:
            continue
        if event.event == EventType.QUEUE_UPDATED:
            item = queue_field(event, "current_item") or {}
            ours = (item.get("media_item") or {}).get("name") == name
            if ours:
                elapsed.append(float(queue_field(event, "elapsed_time")))
                resume.append(int(queue_field(event, "resume_pos")))
        elif event.event == EventType.QUEUE_TIME_UPDATED and ours:
            elapsed.append(float(event.data))
    queue = ctx.client.player_queues.get(queue_id)
    if queue is not None and _current_name(ctx, queue_id) == name:
        elapsed.append(queue.elapsed_time)
        resume.append(queue.resume_pos)
    return max(elapsed, default=0.0), max(resume, default=0)


async def _sleep_until(moment: float) -> None:
    """Sleep until a wall-clock time; return at once when it has passed."""
    await asyncio.sleep(max(moment - time.time(), 0.0))
