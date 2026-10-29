"""Stream checks: flow mode, crossfade, seek and elapsed time, short files and internet radio."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import EventType, PlaybackState, QueueOption
from music_assistant_models.errors import MusicAssistantError

from scenarios.checks.context import check
from scenarios.checks.queue import (
    ADVANCE_MARGIN_S,
    CAPTURE_SETTLE_S,
    COMMAND_TIMEOUT_S,
    HEARD_MIN_S,
    MAX_RESUME_REWIND_S,
    MAX_TONE_GAP_S,
    POLL_S,
    START_TIMEOUT_S,
    TAIL_TONE_S,
    TONE_EDGE_LOSS_S,
    describe,
    duration,
    hear,
    play,
    play_media,
    queue_in,
    queue_left_playing,
    stop,
    title,
    tone_runs,
    tracks,
)
from scenarios.helpers import queue_field, queue_playing, silences, until

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from labctl.analyze import Segment
    from scenarios.checks.context import CheckContext

FLOW_ISSUES = ("#4560", "#6570", "#6353")
FLOW_NEXT_ISSUES = ("server#7147",)
CROSSFADE_ISSUES = ("#4285", "#6440")
SEEK_ISSUES = ("#6798", "#6181")
ELAPSED_ISSUES = ("#5813", "#3704")
END_POSITION_ISSUES = ("#6839",)
SHORT_FILE_ISSUES = ("#3550",)
RADIO_START_ISSUES = ("#5103",)
RADIO_ICY_ISSUES = ("#6542", "#6288", "#4546", "#4090", "#3976", "support#6731")
RADIO_HLS_ISSUES = ("support#6738",)
RADIO_403_ISSUES = ("#6504",)

ALBUM = ("a3t1", "a3t2", "a3t3")
TWO_TRACKS = ("a1t1", "a1t2")
# the server never crossfades two tracks of one album (`allow_crossfade_same_album`)
CROSSFADE_PAIR = ("a1t1", "a2t1")
PLAYED_BEFORE_PAUSE_S = 5.0
# track 1 plays this long before the seek, so the seek does not race the start
PLAYED_BEFORE_SEEK_S = 3.0

FLOW_MODE = "flow_mode"
# protocol outputs show their settings on the visible player as `<protocol id>||protocol||<key>`
PROTOCOL_KEY_SPLITTER = "||protocol||"
QUEUES_DOMAIN = "player_queues"
CROSSFADE_DURATION = "crossfade_duration"
CROSSFADE_MODE = "crossfade_mode"
STANDARD_CROSSFADE = "standard_crossfade"

NO_REPLAY_S = 5.0
PLAYED_BEFORE_NEXT_S = 5.0
# the next skip lands this long before the skipped-to track would end on its own
NEXT_LEAD_S = 3.0
CROSSFADE_S = 4
LISTEN_AFTER_FADE_S = 8.0
# the mixed-span detector trims the edges of the 4 s fade; measured 3.6 s on slim and sendspin
MIN_OVERLAP_S = 3.0
MAX_OVERLAP_S = 5.0
MIN_INCOMING_S = 5.0
LONG_TONE = "a1t4"
SEEK_TARGET_S = 40
MIN_SEEK_REPORTED_S = 39.0
MAX_SEEK_REPORTED_S = 46.0
# sound may stop briefly while the player switches to the seeked stream
SEEK_SETTLE_S = 2.0
SEEK_HEARD_S = 5.0
MAX_SEEK_SILENCE_S = 2.0
# 20 s are left after the seek; a restart from 0 would play about 60 s
MIN_SEEK_END_S = 15.0
MAX_SEEK_END_S = 30.0
FROZEN_FIRST_READ_S = 1.0
FROZEN_LAST_READ_S = 5.0
MAX_FROZEN_DRIFT_S = 1.0
RESUMED_READ_S = 3.0
MIN_RESUMED_ADVANCE_S = 1.5
MAX_RESUMED_ADVANCE_S = 5.0
FRESH_READ_S = 1.5
MAX_FRESH_ELAPSED_S = 4.0
# the elapsed estimate can overshoot the duration by a few hundred ms before the queue goes idle
END_TOLERANCE_S = 1.0
START_HEARD_S = 5.0
# the start chirp sits on the first 200 ms of a track
MARKER_SLACK_S = 0.5
SHORT_PAIR = ("a4t1", "a1t1")
SHORT_HEARD_S = 0.8
SHORT_TIMEOUT_S = 10.0
RADIO_TONE = "a2t4"
ICY_PATH = "/icy"
HLS_PATH = "/hls/live.m3u8"
FLAKY_PATH = "/flaky"
MAX_RADIO_START_S = 4.0
RADIO_HEARD_TIMEOUT_S = 10.0
TITLES_TIMEOUT_S = 25.0
RADIO_TITLE = re.compile(r"Lab Radio \d+")
DROP_RECOVERY_S = 15.0
# past what the player and server buffers hold, so the sound proves a reconnect
DROP_HEARD_AFTER_S = 10.0
HLS_HEARD_TIMEOUT_S = 15.0
HLS_LISTEN_S = 30.0
MAX_HLS_GAP_S = 1.0
# long enough for a retried fetch to have been heard, had the server retried
REFUSED_QUIET_S = 5.0


@check
async def flow_continuous(ctx: CheckContext, role: str) -> None:
    """Check an album plays as one flow stream in order, without silence, and ends for good."""
    queue_id = ctx.player_id(role)
    with ctx.step("flow_mode_set", FLOW_ISSUES):
        restore = await set_flow_mode(ctx, role, True)
    try:
        started = await play(ctx, queue_id, ALBUM, FLOW_ISSUES)
        playing = time.time()
        with ctx.step("ends", FLOW_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                sum(duration(ctx, tone) for tone in ALBUM) + ADVANCE_MARGIN_S,
                since=playing,
            )
            ended = time.time()
        ctx.record_property("album_s", round(ended - started, 1))
        await _record_flow_mode(ctx, queue_id)

        await asyncio.sleep(CAPTURE_SETTLE_S)
        timeline = [seg for seg in ctx.capture(role).timeline() if seg.t1 > started]
        runs = tone_runs(timeline, ALBUM)
        heard = describe(runs, started)
        with ctx.step("continuous", FLOW_ISSUES):
            assert [run.kind for run in runs] == list(ALBUM), f"heard {heard}"
            short = [
                run.kind
                for run in runs
                if run.duration < duration(ctx, run.kind) - TONE_EDGE_LOSS_S
            ]
            assert not short, f"shorter than their tone: {short}; heard {heard}"
            gaps = [
                seg
                for seg in silences(timeline, runs[0].t0, runs[-1].t1)
                if seg.duration >= MAX_TONE_GAP_S
            ]
            assert not gaps, f"silence in the flow stream: {describe(gaps, started)}; heard {heard}"

        with ctx.step("no_tail_replay", FLOW_ISSUES):
            await _stays_stopped(ctx, queue_id, ended)
            tail = _tones_after(ctx, role, runs[-1].t1)
            assert not tail, f"tones after the album ended: {describe(tail, started)}"
    finally:
        await stop(ctx, queue_id)
        if restore is not None:
            await set_flow_mode(ctx, role, restore)


@check
async def flow_next_skips_one(ctx: CheckContext, role: str) -> None:
    """Check next in flow mode moves exactly one track ahead, twice."""
    queue_id = ctx.player_id(role)
    _, second, third = ALBUM
    # third must not start within this window after the first next
    window_s = duration(ctx, second) - NEXT_LEAD_S
    with ctx.step("flow_mode_set", FLOW_NEXT_ISSUES):
        restore = await set_flow_mode(ctx, role, True)
    try:
        await play(ctx, queue_id, ALBUM, FLOW_NEXT_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_NEXT_S)
        await _record_flow_mode(ctx, queue_id)

        skipped = time.time()
        await ctx.client.player_queues.next(queue_id)
        with ctx.step("skips_one", FLOW_NEXT_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 1),
                COMMAND_TIMEOUT_S,
                since=skipped,
            )
            await hear(ctx, role, second, skipped, HEARD_MIN_S, window_s)
            await _sleep_until(skipped + window_s)
            skipped_to_third = [
                seg
                for seg in ctx.capture(role).timeline()
                if seg.kind == third
                and skipped <= seg.t0 < skipped + window_s
                and seg.duration >= TAIL_TONE_S
            ]
            assert not skipped_to_third, (
                f"{third} heard after one next: {describe(skipped_to_third, skipped)}"
            )

        skipped = time.time()
        await ctx.client.player_queues.next(queue_id)
        with ctx.step("skips_second", FLOW_NEXT_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 2),
                COMMAND_TIMEOUT_S,
                since=skipped,
            )
            await hear(ctx, role, third, skipped, HEARD_MIN_S, window_s)
    finally:
        await stop(ctx, queue_id)
        if restore is not None:
            await set_flow_mode(ctx, role, restore)


@check
async def crossfade_overlap(ctx: CheckContext, role: str) -> None:
    """Check a 4 s standard crossfade overlaps two tracks with no gap and no skip."""
    queue_id = ctx.player_id(role)
    first, second = CROSSFADE_PAIR
    previous = await _save_queue_settings(
        ctx, {CROSSFADE_DURATION: CROSSFADE_S, CROSSFADE_MODE: STANDARD_CROSSFADE}
    )
    try:
        await _set_crossfade(ctx, queue_id, True)
        started = await play(ctx, queue_id, CROSSFADE_PAIR, CROSSFADE_ISSUES)
        with ctx.step("advances", CROSSFADE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 1),
                duration(ctx, first) + ADVANCE_MARGIN_S,
                since=started,
            )
        await asyncio.sleep(LISTEN_AFTER_FADE_S)

        capture = ctx.capture(role)
        timeline = [seg for seg in capture.timeline() if seg.t1 > started]
        runs = tone_runs(timeline, CROSSFADE_PAIR)
        heard = describe(tone_runs(timeline, (*CROSSFADE_PAIR, "mixed")), started)
        with ctx.step("overlaps", CROSSFADE_ISSUES):
            # the end and start chirps can split one crossfade into several mixed spans
            fades = [
                fade
                for fade in capture.overlaps()
                if fade.t1 > started and (fade.outgoing, fade.incoming) == CROSSFADE_PAIR
            ]
            assert fades, f"no {first} -> {second} crossfade; heard {heard}"
            fade_s = fades[-1].t1 - fades[0].t0
            ctx.record_property("crossfade_s", round(fade_s, 2))
            assert MIN_OVERLAP_S <= fade_s <= MAX_OVERLAP_S, (
                f"crossfade of {fade_s:.1f} s, expected {MIN_OVERLAP_S} to {MAX_OVERLAP_S} s"
            )

        outgoing = [run for run in runs if run.kind == first]
        incoming = [run for run in runs if run.kind == second]
        with ctx.step("no_gap", CROSSFADE_ISSUES):
            assert outgoing and incoming, f"heard {heard}"
            gaps = [
                seg
                for seg in silences(timeline, outgoing[-1].t1, incoming[0].t0)
                if seg.duration >= MAX_TONE_GAP_S
            ]
            assert not gaps, f"silence between the tracks: {describe(gaps, started)}"

        with ctx.step("no_skip", CROSSFADE_ISSUES):
            assert incoming[0].duration >= MIN_INCOMING_S, (
                f"{second} played {incoming[0].duration:.1f} s; heard {heard}"
            )
    finally:
        await stop(ctx, queue_id)
        await _set_crossfade(ctx, queue_id, False)
        await _save_queue_settings(ctx, previous)


@check
async def seek_resumes(ctx: CheckContext, role: str) -> None:
    """Check a seek reports the new position, keeps sounding and plays on from there."""
    queue_id = ctx.player_id(role)
    try:
        await play(ctx, queue_id, (LONG_TONE,), SEEK_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_SEEK_S)
        seeked = time.time()
        await ctx.client.player_queues.seek(queue_id, SEEK_TARGET_S)

        with ctx.step("position_reported", SEEK_ISSUES):
            reported: list[float] = []

            def at_target() -> bool:
                reported[:] = [_elapsed(ctx, queue_id)]
                return MIN_SEEK_REPORTED_S <= reported[0] <= MAX_SEEK_REPORTED_S

            try:
                await until(at_target, COMMAND_TIMEOUT_S, POLL_S)
            finally:
                if reported:
                    ctx.record_property("seek_reported_s", round(reported[0], 1))

        with ctx.step("audio_continues", SEEK_ISSUES):
            await _sleep_until(seeked + SEEK_HEARD_S + CAPTURE_SETTLE_S)
            timeline = ctx.capture(role).timeline()
            t0, t1 = seeked + SEEK_SETTLE_S, seeked + SEEK_HEARD_S
            heard = [
                seg for seg in timeline if seg.kind == LONG_TONE and seg.t1 > t0 and seg.t0 < t1
            ]
            assert heard, f"{LONG_TONE} not heard {SEEK_SETTLE_S}-{SEEK_HEARD_S} s after the seek"
            stalls = [
                seg for seg in silences(timeline, t0, t1) if seg.duration >= MAX_SEEK_SILENCE_S
            ]
            assert not stalls, f"silent after the seek: {describe(stalls, seeked)}"

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
        await stop(ctx, queue_id)


@check
async def elapsed_tracks_pause(ctx: CheckContext, role: str) -> None:
    """Check elapsed time stands still while paused, runs on after resume and resets per track."""
    queue_id = ctx.player_id(role)
    first, second = TWO_TRACKS
    try:
        await play(ctx, queue_id, (first,), ELAPSED_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_PAUSE_S)

        paused = time.time()
        await ctx.client.player_queues.pause(queue_id)
        with ctx.step("elapsed_frozen", ELAPSED_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                COMMAND_TIMEOUT_S,
                since=paused,
            )
            await _sleep_until(paused + FROZEN_FIRST_READ_S)
            early = _elapsed(ctx, queue_id)
            await _sleep_until(paused + FROZEN_LAST_READ_S)
            frozen = _elapsed(ctx, queue_id)
            ctx.record_property("paused_elapsed_s", round(frozen, 1))
            assert abs(frozen - early) < MAX_FROZEN_DRIFT_S, (
                f"elapsed moved from {early:.1f} to {frozen:.1f} s while paused"
            )

        resumed = time.time()
        await ctx.client.player_queues.resume(queue_id)
        with ctx.step("elapsed_continues", ELAPSED_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 0),
                COMMAND_TIMEOUT_S,
                since=resumed,
            )
            await _sleep_until(resumed + RESUMED_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            low = frozen - MAX_RESUME_REWIND_S + MIN_RESUMED_ADVANCE_S
            high = frozen + MAX_RESUMED_ADVANCE_S
            assert low <= elapsed <= high, (
                f"elapsed {elapsed:.1f} s {RESUMED_READ_S} s after resume, expected {low:.1f}"
                f" to {high:.1f} s"
            )

        media = await tracks(ctx, (second,))
        replaced = time.time()
        await ctx.client.player_queues.play_media(queue_id, media, QueueOption.REPLACE)
        with ctx.step("fresh_starts_at_zero", ELAPSED_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                _playing_track(queue_id, title(ctx, second)),
                START_TIMEOUT_S,
                since=replaced,
            )
            await asyncio.sleep(FRESH_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            assert elapsed < MAX_FRESH_ELAPSED_S, (
                f"{second} reports {elapsed:.1f} s elapsed {FRESH_READ_S} s after it started"
            )
    finally:
        await stop(ctx, queue_id)


@check
async def stop_after_end_position(ctx: CheckContext, role: str) -> None:
    """Check a track that played to its end never reports a position past it and replays from 0."""
    queue_id = ctx.player_id(role)
    first = TWO_TRACKS[0]
    track_s = duration(ctx, first)
    try:
        started = await play(ctx, queue_id, (first,), END_POSITION_ISSUES)
        playing = time.time()
        with ctx.step("position_not_past_end", END_POSITION_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                track_s + ADVANCE_MARGIN_S,
                since=playing,
            )
            await asyncio.sleep(CAPTURE_SETTLE_S)
            elapsed, resume_pos = _reported_positions(ctx, queue_id, started)
            ctx.record_property("max_elapsed_s", round(elapsed, 1))
            ctx.record_property("max_resume_pos_s", resume_pos)
            assert elapsed <= track_s + END_TOLERANCE_S, (
                f"elapsed reached {elapsed:.1f} s of a {track_s} s track"
            )
            assert 0 <= resume_pos <= track_s, f"resume position {resume_pos} s"

        replayed = await play(ctx, queue_id, (first,), END_POSITION_ISSUES)
        with ctx.step("next_play_starts_at_zero", END_POSITION_ISSUES):
            await asyncio.sleep(FRESH_READ_S)
            elapsed = _elapsed(ctx, queue_id)
            assert elapsed < MAX_FRESH_ELAPSED_S, f"replay reports {elapsed:.1f} s elapsed"
            await _sleep_until(replayed + START_HEARD_S + CAPTURE_SETTLE_S)
            capture = ctx.capture(role)
            tone_ids = {tone.id for tone in ctx.tones.tracks}
            sound = [
                seg
                for seg in capture.timeline()
                if seg.kind in tone_ids and seg.t0 >= replayed and seg.duration >= TAIL_TONE_S
            ]
            assert sound, f"nothing heard within {START_HEARD_S + CAPTURE_SETTLE_S} s of the replay"
            heard = sound[0]
            assert heard.kind == first and heard.t0 <= replayed + START_HEARD_S, (
                f"first heard {describe(sound[:1], replayed)}"
            )
            assert any(abs(marker - heard.t0) <= MARKER_SLACK_S for marker in capture.markers()), (
                f"{first} at {heard.t0 - replayed:.1f} s has no start marker"
            )
    finally:
        await stop(ctx, queue_id)


@check
async def short_file_plays(ctx: CheckContext, role: str) -> None:
    """Check a 1.5 s track is heard and the queue advances past it."""
    queue_id = ctx.player_id(role)
    short, after = SHORT_PAIR
    try:
        media = await tracks(ctx, SHORT_PAIR)
        started = time.time()
        # the short track may be over before the queue reports index 0, so any index counts
        with ctx.step("starts", SHORT_FILE_ISSUES):
            await ctx.client.player_queues.play_media(queue_id, media, QueueOption.REPLACE)
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_in(queue_id, "playing"),
                START_TIMEOUT_S,
                since=started,
            )
        with ctx.step("short_plays", SHORT_FILE_ISSUES):
            await hear(ctx, role, short, started, SHORT_HEARD_S, SHORT_TIMEOUT_S)
        with ctx.step("advances_past_short", SHORT_FILE_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED, queue_playing(queue_id, 1), SHORT_TIMEOUT_S, since=started
            )
            await hear(ctx, role, after, started, HEARD_MIN_S, SHORT_TIMEOUT_S)
    finally:
        await stop(ctx, queue_id)


@check
async def radio_start_latency(ctx: CheckContext, role: str) -> None:
    """Check an ICY radio stream is heard within 4 s of the play command."""
    queue_id = ctx.player_id(role)
    try:
        started = await play_media(ctx, queue_id, radio_url(ICY_PATH), RADIO_START_ISSUES)
        with ctx.step("starts_fast", RADIO_START_ISSUES):
            heard = await hear(ctx, role, RADIO_TONE, started, HEARD_MIN_S, RADIO_HEARD_TIMEOUT_S)
            latency = heard.t0 - started
            ctx.record_property("radio_start_s", round(latency, 2))
            assert latency <= MAX_RADIO_START_S, f"radio heard {latency:.1f} s after play"
    finally:
        await stop(ctx, queue_id)


@check
async def radio_icy_plays(ctx: CheckContext, role: str) -> None:
    """Check an ICY stream plays, follows its stream titles and survives a dropped connection."""
    queue_id = ctx.player_id(role)
    try:
        started = await play_media(ctx, queue_id, radio_url(ICY_PATH), RADIO_ICY_ISSUES)
        with ctx.step("plays", RADIO_ICY_ISSUES):
            await hear(ctx, role, RADIO_TONE, started, HEARD_MIN_S, RADIO_HEARD_TIMEOUT_S)

        with ctx.step("metadata_updates", RADIO_ICY_ISSUES):
            titles: list[str] = []

            def two_titles() -> bool:
                current = _stream_title(ctx, queue_id)
                if current and RADIO_TITLE.fullmatch(current) and current not in titles:
                    titles.append(current)
                return len(titles) >= 2

            try:
                await until(two_titles, TITLES_TIMEOUT_S, POLL_S)
            finally:
                ctx.record_property("stream_titles", ", ".join(titles))

        dropped = time.time()
        reply = await _radio_control("POST", "/control/drop")
        with ctx.step("survives_drop", RADIO_ICY_ISSUES):
            assert reply["dropped"] >= 1, "no radio stream was open to drop"

            def heard_after_drop() -> bool:
                return any(
                    seg.kind == RADIO_TONE
                    and seg.t1 >= dropped + DROP_HEARD_AFTER_S
                    and seg.duration >= HEARD_MIN_S
                    for seg in ctx.capture(role, dropped).timeline()
                )

            await until(heard_after_drop, DROP_RECOVERY_S + CAPTURE_SETTLE_S, POLL_S)
            state = await _radio_control("GET", "/control/state")
            assert state["connections"] >= 1, "the server did not reconnect to the radio"
    finally:
        await stop(ctx, queue_id)


@check
async def radio_hls_plays(ctx: CheckContext, role: str) -> None:
    """Check a live HLS stream plays for 30 s without stalling."""
    queue_id = ctx.player_id(role)
    try:
        started = await play_media(ctx, queue_id, radio_url(HLS_PATH), RADIO_HLS_ISSUES)
        with ctx.step("plays", RADIO_HLS_ISSUES):
            first = await hear(ctx, role, RADIO_TONE, started, HEARD_MIN_S, HLS_HEARD_TIMEOUT_S)

        with ctx.step("no_stall", RADIO_HLS_ISSUES):
            await _sleep_until(first.t0 + HLS_LISTEN_S + CAPTURE_SETTLE_S)
            capture = ctx.capture(role)
            assert capture.ended is None, f"capture ended at {capture.ended}"
            stalls = [
                seg
                for seg in silences(capture.timeline(), first.t0, first.t0 + HLS_LISTEN_S)
                if seg.duration >= MAX_HLS_GAP_S
            ]
            assert not stalls, f"HLS stalled: {describe(stalls, started)}"
    finally:
        await stop(ctx, queue_id)


@check
async def radio_403_rejected(ctx: CheckContext, role: str) -> None:
    """Check a stream that answers 403 is refused at once and the player stays silent."""
    queue_id = ctx.player_id(role)
    try:
        await _radio_control("POST", "/control/reset")
        # a 403 is final by design (#6504 closed as working as designed): no retry, a clear error
        with ctx.step("refused", RADIO_403_ISSUES):
            asked = time.time()
            try:
                await ctx.client.player_queues.play_media(
                    queue_id, radio_url(FLAKY_PATH), QueueOption.REPLACE
                )
            except MusicAssistantError as err:
                ctx.record_property("refusal", type(err).__name__)
                ctx.record_property("refused_after_s", round(time.time() - asked, 2))
            else:
                raise AssertionError("the 403 stream was accepted")

        with ctx.step("stays_quiet", RADIO_403_ISSUES):
            await _sleep_until(asked + REFUSED_QUIET_S)
            heard = [
                run
                for run in tone_runs(ctx.capture(role).timeline(), (RADIO_TONE,))
                if run.t1 > asked
            ]
            assert not heard, f"the refused stream was heard: {describe(heard, asked)}"
            player = ctx.client.players.get(queue_id)
            assert player is not None and player.playback_state != PlaybackState.PLAYING, (
                "the player plays after the refused stream"
            )
    finally:
        await stop(ctx, queue_id)


async def set_flow_mode(ctx: CheckContext, role: str, enabled: bool) -> bool | None:
    """Set flow mode on the role's player; return the old value, None when it has no flow mode."""
    player_id = ctx.player_id(role)
    values = await _flow_mode_values(ctx, player_id)
    ctx.record_property("flow_mode_keys", ", ".join(values) or "none")
    if not values:
        return None
    previous = next(iter(values.values()))
    await ctx.client.send_command(
        "config/players/save", player_id=player_id, values=dict.fromkeys(values, enabled)
    )
    stored = await _flow_mode_values(ctx, player_id)
    unchanged = {key: value for key, value in stored.items() if value != enabled}
    assert not unchanged, f"flow mode {enabled} did not stick on {player_id}: {unchanged}"

    def available() -> bool:
        player = ctx.client.players.get(player_id)
        return player is not None and player.available

    await until(available, START_TIMEOUT_S, POLL_S)
    return previous


def radio_url(path: str) -> str:
    """URL of a path on the lab radio."""
    return os.environ["LAB_RADIO_URL"] + path


async def _flow_mode_values(ctx: CheckContext, player_id: str) -> dict[str, bool]:
    """Return the stored flow mode per flow mode key of the player, protocol outputs included."""
    config = await ctx.client.send_command("config/players/get", player_id=player_id)
    return {
        key: bool(entry["default_value"] if entry["value"] is None else entry["value"])
        for key, entry in config["values"].items()
        if key == FLOW_MODE or key.endswith(PROTOCOL_KEY_SPLITTER + FLOW_MODE)
    }


async def _record_flow_mode(ctx: CheckContext, queue_id: str) -> None:
    """Record whether the server streams the queue in flow mode."""
    # the server sets the flag once the flow stream starts, without a queue update to clients
    queue = await ctx.client.send_command("player_queues/get", queue_id=queue_id)
    ctx.record_property("flow_mode", bool(queue and queue["flow_mode"]))


async def _stays_stopped(ctx: CheckContext, queue_id: str, ended: float) -> None:
    """Fail when the queue plays again within 5 s of `ended`."""
    try:
        await ctx.events.wait_for(
            EventType.QUEUE_UPDATED,
            queue_in(queue_id, "playing"),
            max(ended + NO_REPLAY_S - time.time(), 0.0),
            since=ended,
        )
    except AssertionError:
        return
    raise AssertionError(f"queue played again within {NO_REPLAY_S} s of ending")


def _tones_after(ctx: CheckContext, role: str, after: float) -> list[Segment]:
    """Tone segments of at least 0.5 s starting at or after `after`."""
    tone_ids = {tone.id for tone in ctx.tones.tracks}
    return [
        seg
        for seg in ctx.capture(role).timeline()
        if seg.kind in tone_ids and seg.t0 >= after and seg.duration >= TAIL_TONE_S
    ]


async def _save_queue_settings(ctx: CheckContext, values: dict[str, Any]) -> dict[str, Any]:
    """Save global queue settings; return the values they replace."""
    previous = {
        key: await ctx.client.send_command("config/core/get_value", domain=QUEUES_DOMAIN, key=key)
        for key in values
    }
    await ctx.client.send_command("config/core/save", domain=QUEUES_DOMAIN, values=values)
    return previous


async def _set_crossfade(ctx: CheckContext, queue_id: str, enabled: bool) -> None:
    """Toggle crossfade on the queue."""
    await ctx.client.send_command(
        "player_queues/crossfade", queue_id=queue_id, crossfade_enabled=enabled
    )


def _elapsed(ctx: CheckContext, queue_id: str) -> float:
    """Elapsed time of the queue's current item as the client sees it now."""
    queue = ctx.client.player_queues.get(queue_id)
    assert queue is not None, f"queue {queue_id} unknown"
    return float(queue.corrected_elapsed_time)


def _reported_positions(ctx: CheckContext, queue_id: str, since: float) -> tuple[float, int]:
    """Highest elapsed time and resume position the queue reported since `since`."""
    elapsed: list[float] = []
    resume: list[int] = []
    for recorded in ctx.events.events:
        event = recorded.event
        if recorded.time < since or event.object_id != queue_id:
            continue
        if event.event == EventType.QUEUE_UPDATED:
            elapsed.append(float(queue_field(event, "elapsed_time")))
            resume.append(int(queue_field(event, "resume_pos")))
        elif event.event == EventType.QUEUE_TIME_UPDATED:
            elapsed.append(float(event.data))
    queue = ctx.client.player_queues.get(queue_id)
    if queue is not None:
        elapsed.append(queue.elapsed_time)
        resume.append(queue.resume_pos)
    return max(elapsed, default=0.0), max(resume, default=0)


def _playing_track(queue_id: str, track_title: str) -> Callable[[MassEvent], bool]:
    """Match a QUEUE_UPDATED event of the queue playing the track titled `track_title`."""

    def playing(event: MassEvent) -> bool:
        item = queue_field(event, "current_item")
        media_item = item.get("media_item") if item else None
        return (
            event.object_id == queue_id
            and queue_field(event, "state") == "playing"
            and media_item is not None
            and media_item["name"] == track_title
        )

    return playing


def _stream_title(ctx: CheckContext, queue_id: str) -> str | None:
    """Stream title of the queue's current item, as parsed from in-band radio metadata."""
    queue = ctx.client.player_queues.get(queue_id)
    item = queue.current_item if queue is not None else None
    if item is None or item.streamdetails is None:
        return None
    return item.streamdetails.stream_title


async def _radio_control(method: str, path: str) -> dict[str, Any]:
    """Call a control endpoint of the lab radio and return its JSON reply."""
    timeout = aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_S)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.request(method, radio_url(path)) as response,
    ):
        response.raise_for_status()
        reply: dict[str, Any] = await response.json()
        return reply


async def _sleep_until(moment: float) -> None:
    """Sleep until a wall-clock time; return at once when it has passed."""
    await asyncio.sleep(max(moment - time.time(), 0.0))
