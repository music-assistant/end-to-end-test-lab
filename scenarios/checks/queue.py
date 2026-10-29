"""Queue checks: an album plays through, pause and resume keep moving, now playing follows."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable, Collection, Iterable
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, PlaybackState, QueueOption

from labctl.analyze import WINDOW
from scenarios.helpers import queue_field, queue_playing, until

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import Track

    from labctl.analyze import Segment
    from scenarios.checks.context import CheckContext

ALBUM_ISSUES = ("#6666", "#6562", "#6359", "#4460", "#1962", "#4337", "#4789", "#5003", "#6353")
RESUME_ISSUES = ("#6160", "#3758")
NOW_PLAYING_ISSUES = ("#6025", "#4183")
NEXT_ISSUES = ("server#7254",)

ALBUM = ("a3t1", "a3t2", "a3t3")
TWO_TRACKS = ("a3t1", "a3t2")
START_TIMEOUT_S = 15.0
# slack on top of a track's length for the queue to report the next item or its end
ADVANCE_MARGIN_S = 15.0
COMMAND_TIMEOUT_S = 5.0
# the capture trails the queue by the player's buffer
CAPTURE_SETTLE_S = 3.0
# once the player reports stopped, the queue's 5 s end-of-queue check is armed; a play inside
# that window can land on index 1 (server#7278, closed as unrealistic), so journeys pace like a user
STOP_SETTLE_S = 5.5
# fades and analysis window edges cost a little of each tone
TONE_EDGE_LOSS_S = 2.0
# flow playback is gapless; one analysis window is 85 ms, so anything past three is a real hole
MAX_TONE_GAP_S = 0.25
# silence, noise or crossfade runs shorter than this do not split a tone
MERGE_GAP_S = 1.0
TAIL_TONE_S = 0.5
PLAYED_BEFORE_PAUSE_S = 3.0
PAUSED_S = 3.0
# sound may outlive a pause by the player's buffer
PAUSE_SILENT_WITHIN_S = 2.0
# a resume restarts at the whole-second resume position when the player cannot unpause in place
MAX_RESUME_REWIND_S = 1.0
HEARD_MIN_S = 1.0
# a capture loaded from `since` stamps a tone already playing at its start, so the load begins two
# analysis windows (at 44.1 kHz or more) before the tolerance of starts_after
HEAR_LEAD_S = 0.2
HEARD_TIMEOUT_S = 10.0
NOW_PLAYING_HEARD_S = 0.5
# the server moves a flow queue's index on its 1 s player poll after a 0.5 s debounce; measured
# -0.2 to 1.4 s over 78 reads on dlna, slim, snap and sendspin (2026-10-28), airplay reports early
MAX_NOW_PLAYING_LAG_S = 2.0
POLL_S = 0.25
PLAYED_BEFORE_NEXT_S = 6.0
# the skipped track plays on until the queue hands the player the next item (~1 s)
MAX_STALE_AFTER_NEXT_S = 1.5
MAX_NEXT_HEARD_S = 3.0


async def album_plays_through(ctx: CheckContext, role: str) -> None:
    """Check three album tracks play in order to the end of the queue, with no tail replayed."""
    queue_id = ctx.player_id(role)

    def player_left_playing() -> bool:
        player = ctx.client.players.get(queue_id)
        return player is not None and player.playback_state != PlaybackState.PLAYING

    try:
        started = await play(ctx, queue_id, ALBUM, ALBUM_ISSUES)
        with ctx.step("advances", ALBUM_ISSUES):
            for index in range(1, len(ALBUM)):
                await ctx.events.wait_for(
                    EventType.QUEUE_UPDATED,
                    queue_playing(queue_id, index),
                    duration(ctx, ALBUM[index - 1]) + ADVANCE_MARGIN_S,
                    since=started,
                )
            last_started = time.time()

        with ctx.step("ends", ALBUM_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                duration(ctx, ALBUM[-1]) + ADVANCE_MARGIN_S,
                since=last_started,
            )
            ended = time.time()
            await until(player_left_playing, COMMAND_TIMEOUT_S, POLL_S)
        ctx.record_property("album_s", round(ended - started, 1))

        await asyncio.sleep(CAPTURE_SETTLE_S)
        timeline = [seg for seg in ctx.capture(role).timeline() if seg.t1 > started]
        runs = tone_runs(timeline, ALBUM)
        heard = describe(runs, started)
        with ctx.step("tones_in_order", ALBUM_ISSUES):
            assert [run.kind for run in runs] == list(ALBUM), f"heard {heard}"
            short = [
                run.kind
                for run in runs
                if run.duration < duration(ctx, run.kind) - TONE_EDGE_LOSS_S
            ]
            assert not short, (
                f"over {TONE_EDGE_LOSS_S} s shorter than the track: {short}; heard {heard}"
            )
            gaps = [
                f"{a.kind}->{b.kind} {b.t0 - a.t1:.1f} s"
                for a, b in itertools.pairwise(runs)
                if b.t0 - a.t1 > MAX_TONE_GAP_S
            ]
            assert not gaps, f"gaps over {MAX_TONE_GAP_S} s: {gaps}; heard {heard}"

        with ctx.step("no_tail_replay", ALBUM_ISSUES):
            tone_ids = {tone.id for tone in ctx.tones.tracks}
            tail = [
                seg
                for seg in timeline
                if seg.kind in tone_ids and seg.t0 >= runs[-1].t1 and seg.duration >= TAIL_TONE_S
            ]
            assert not tail, f"tones after the album ended: {describe(tail, started)}"
    finally:
        await stop(ctx, queue_id)


async def resume_advances(ctx: CheckContext, role: str) -> None:
    """Check a paused track goes silent, resumes and still advances to the next item."""
    queue_id = ctx.player_id(role)
    first, second = TWO_TRACKS
    try:
        await play(ctx, queue_id, TWO_TRACKS, RESUME_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_PAUSE_S)

        paused = time.time()
        await ctx.client.player_queues.pause(queue_id)
        with ctx.step("pauses", RESUME_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_left_playing(queue_id),
                COMMAND_TIMEOUT_S,
                since=paused,
            )
            await asyncio.sleep(CAPTURE_SETTLE_S)
            sound = [
                seg
                for seg in ctx.capture(role).timeline()
                if seg.kind != "silence" and seg.t1 > paused + PAUSE_SILENT_WITHIN_S
            ]
            assert not sound, f"sound after pause: {describe(sound, paused)}"

        await asyncio.sleep(PAUSED_S)
        resumed = time.time()
        await ctx.client.player_queues.resume(queue_id)
        with ctx.step("resumes", RESUME_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 0),
                COMMAND_TIMEOUT_S,
                since=resumed,
            )
            await hear(ctx, role, first, resumed, HEARD_MIN_S, HEARD_TIMEOUT_S)

        with ctx.step("advances", RESUME_ISSUES):
            await ctx.events.wait_for(
                EventType.QUEUE_UPDATED,
                queue_playing(queue_id, 1),
                duration(ctx, first) + ADVANCE_MARGIN_S,
                since=resumed,
            )
            await hear(ctx, role, second, resumed, HEARD_MIN_S, HEARD_TIMEOUT_S)
    finally:
        await stop(ctx, queue_id)


async def next_drops_previous(ctx: CheckContext, role: str) -> None:
    """Check next mid-track silences the skipped track at once and plays the next within 3 s."""
    queue_id = ctx.player_id(role)
    first, second = TWO_TRACKS
    try:
        await play(ctx, queue_id, TWO_TRACKS, NEXT_ISSUES)
        await asyncio.sleep(PLAYED_BEFORE_NEXT_S)
        skipped = time.time()
        await ctx.client.player_queues.next(queue_id)
        with ctx.step("next_heard", NEXT_ISSUES):
            heard = await hear(ctx, role, second, skipped, HEARD_MIN_S, HEARD_TIMEOUT_S)
            ctx.record_property("next_heard_s", round(heard.t0 - skipped, 2))
            assert heard.t0 - skipped <= MAX_NEXT_HEARD_S, (
                f"{second} heard {heard.t0 - skipped:.1f} s after next"
            )
        with ctx.step("previous_dropped", NEXT_ISSUES):
            stale = [
                seg
                for seg in tone_runs(ctx.capture(role).timeline(), (first,))
                if seg.t1 > skipped + MAX_STALE_AFTER_NEXT_S
            ]
            assert not stale, f"{first} heard after next: {describe(stale, skipped)}"
    finally:
        await stop(ctx, queue_id)


async def now_playing_follows(ctx: CheckContext, role: str) -> None:
    """Check the queue reports track 2 as now playing within 2 s of it being heard."""
    queue_id = ctx.player_id(role)
    first, second = TWO_TRACKS
    name = title(ctx, second)
    timeout = duration(ctx, first) + ADVANCE_MARGIN_S

    def reports_track_2(event: MassEvent) -> bool:
        item = queue_field(event, "current_item")
        media_item = item.get("media_item") if item is not None else None
        return (
            event.object_id == queue_id
            and queue_field(event, "current_index") == 1
            and media_item is not None
            and media_item["name"] == name
        )

    try:
        started = await play(ctx, queue_id, TWO_TRACKS, NOW_PLAYING_ISSUES)
        # from the moment index 0 plays: the replace's first events still carry the previous
        # check's index and item
        watched = time.time()
        with ctx.step("reports_track_2", NOW_PLAYING_ISSUES):
            # watched alongside the capture, so a report before the audio counts as early
            reporting = asyncio.create_task(
                ctx.events.wait_for(
                    EventType.QUEUE_UPDATED,
                    reports_track_2,
                    timeout + MAX_NOW_PLAYING_LAG_S,
                    since=watched,
                )
            )
            try:
                heard = await hear(ctx, role, second, started, NOW_PLAYING_HEARD_S, timeout)
                event = await reporting
            finally:
                reporting.cancel()
            reported = next(r.time for r in ctx.events.events if r.event is event)
            lag = reported - heard.t0
            ctx.record_property("now_playing_lag_s", round(lag, 2))
            assert lag <= MAX_NOW_PLAYING_LAG_S, (
                f"now playing reported track 2 {lag:.1f} s after it was heard"
            )
    finally:
        await stop(ctx, queue_id)


def tone_runs(timeline: Iterable[Segment], kinds: Collection[str]) -> list[Segment]:
    """Merge the segments of `kinds` into one per heard tone, bridging runs shorter than 1 s."""
    runs: list[Segment] = []
    for seg in timeline:
        if seg.kind not in kinds:
            continue
        if runs and runs[-1].kind == seg.kind and seg.t0 - runs[-1].t1 < MERGE_GAP_S:
            runs[-1] = runs[-1]._replace(t1=seg.t1)
        else:
            runs.append(seg)
    return runs


async def play(
    ctx: CheckContext, queue_id: str, tone_ids: tuple[str, ...], issues: tuple[str, ...]
) -> float:
    """Replace the queue with the tones, wait in step `starts` until it plays; return play time."""
    return await play_media(ctx, queue_id, await tracks(ctx, tone_ids), issues)


async def play_media(
    ctx: CheckContext, queue_id: str, media: list[Track] | str, issues: tuple[str, ...]
) -> float:
    """Replace the queue with `media`, wait in step `starts` until it plays; return play time."""
    started = time.time()
    with ctx.step("starts", issues):
        await ctx.client.player_queues.play_media(queue_id, media, QueueOption.REPLACE)
        await ctx.events.wait_for(
            EventType.QUEUE_UPDATED, queue_playing(queue_id, 0), START_TIMEOUT_S, since=started
        )
    return started


async def tracks(ctx: CheckContext, tone_ids: tuple[str, ...]) -> list[Track]:
    """Library tracks of the tones, looked up by title."""
    found: list[Track] = []
    for tone_id in tone_ids:
        [track] = await ctx.client.music.get_library_tracks(search=title(ctx, tone_id), limit=1)
        found.append(track)
    return found


def title(ctx: CheckContext, tone_id: str) -> str:
    """Title of a tone in the manifest."""
    return next(tone.title for tone in ctx.tones.tracks if tone.id == tone_id)


def duration(ctx: CheckContext, tone_id: str) -> float:
    """Length in seconds of a tone in the manifest."""
    return next(tone.duration_s for tone in ctx.tones.tracks if tone.id == tone_id)


async def hear(
    ctx: CheckContext, role: str, tone_id: str, after: float, min_s: float, timeout: float
) -> Segment:
    """Wait until the capture has a segment of the tone starting after `after`; return it."""
    found: list[Segment] = []

    def tone_heard() -> bool:
        capture = ctx.capture(role, after - HEAR_LEAD_S)
        window_s = WINDOW / capture.rate
        found[:] = [
            seg
            for seg in capture.timeline()
            if seg.kind == tone_id and starts_after(seg, after, window_s) and seg.duration >= min_s
        ][:1]
        return bool(found)

    await until(tone_heard, timeout, POLL_S)
    return found[0]


def starts_after(seg: Segment, after: float, window_s: float) -> bool:
    """Whether the segment starts at or after `after`, within one analysis window."""
    # a tone starting in the command's own window is stamped at that window's start
    return seg.t0 >= after - window_s


async def stop(ctx: CheckContext, queue_id: str, timeout: float = COMMAND_TIMEOUT_S) -> None:
    """Stop the queue, wait until it left playing and let the server settle before the next one."""
    await ctx.client.player_queues.stop(queue_id)
    # no assert: stop runs in every check's finally and must not mask the failed step
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        queue = ctx.client.player_queues.get(queue_id)
        if queue is None or queue.state != PlaybackState.PLAYING:
            break
        await asyncio.sleep(POLL_S)
    await asyncio.sleep(STOP_SETTLE_S)


def queue_in(queue_id: str, *states: str) -> Callable[[MassEvent], bool]:
    """Match a QUEUE_UPDATED event of the queue in one of `states`."""

    def in_states(event: MassEvent) -> bool:
        return event.object_id == queue_id and queue_field(event, "state") in states

    in_states.__name__ = f"queue {' or '.join(states)}"
    return in_states


def queue_left_playing(queue_id: str) -> Callable[[MassEvent], bool]:
    """Match a QUEUE_UPDATED event of the queue in any state but playing."""

    def left_playing(event: MassEvent) -> bool:
        return event.object_id == queue_id and queue_field(event, "state") != "playing"

    return left_playing


def describe(segments: Iterable[Segment], origin: float) -> str:
    """Segments as `kind start-end` in seconds after `origin`."""
    return ", ".join(f"{s.kind} {s.t0 - origin:.1f}-{s.t1 - origin:.1f}" for s in segments)
