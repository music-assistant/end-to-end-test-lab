"""The last queue item plays to its end and the queue stops cleanly (#6860, #6667, #6222)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from labctl.analyze import Capture
from scenarios.helpers import (
    describe,
    ma_log_errors,
    ma_log_size,
    pick_tracks,
    queue_field,
    queue_playing,
    silences,
)

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient
    from music_assistant_models.event import MassEvent

    from scenarios.conftest import EventRecorder

pytestmark = [pytest.mark.tier2, pytest.mark.timeout(180)]

# the seek leaves this much of the track, so the test sees its natural end quickly
TAIL_S = 20
START_TIMEOUT_S = 30.0
# the track plays this long before the seek, so the seek does not race the start
PLAYED_BEFORE_SEEK_S = 3.0
SEEK_TIMEOUT_S = 15.0
END_MARGIN_S = 15.0
# reported duration, seek and elapsed time are whole seconds and a fade out can dip below -60 dB
END_TOLERANCE_S = 3.0
MAX_TRACK_SILENCE_S = 1.0
# sound later than this after the expected end is the track played again
REPLAY_AFTER_S = 5.0
REPLAY_WATCH_S = 10.0


async def test_last_track_ends_clean(
    role: str,
    client: MusicAssistantClient,
    players: dict[str, str],
    events: EventRecorder,
    capture: Callable[[str], Capture],
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    """A single queued track plays to its end, the queue goes idle and nothing plays after."""
    from music_assistant_models.enums import EventType, QueueOption

    player_id = players[role]
    [track] = await pick_tracks(client, 1)
    record_testsuite_property(f"last_track_ends_clean[{role}] track", describe(track))
    assert track.duration is not None
    log_offset = ma_log_size()

    def time_from(minimum: float) -> Callable[[MassEvent], bool]:
        def reached(event: MassEvent) -> bool:
            return event.object_id == player_id and float(event.data) >= minimum

        return reached

    started = time.time()
    try:
        await client.player_queues.play_media(player_id, track, QueueOption.REPLACE)
        await events.wait_for(
            EventType.QUEUE_UPDATED, queue_playing(player_id, 0), START_TIMEOUT_S, since=started
        )
        await asyncio.sleep(PLAYED_BEFORE_SEEK_S)

        position = track.duration - TAIL_S
        seeked = time.time()
        await client.player_queues.seek(player_id, position)
        at_seek = await events.wait_for(
            EventType.QUEUE_TIME_UPDATED, time_from(position - 1), SEEK_TIMEOUT_S, since=seeked
        )
        resumed = time.time()
        expected_end = resumed + track.duration - float(at_seek.data)

        await events.wait_for(
            EventType.QUEUE_UPDATED,
            lambda e: e.object_id == player_id and queue_field(e, "state") == "idle",
            TAIL_S + END_MARGIN_S,
            since=resumed,
        )
        listen_end = expected_end + REPLAY_AFTER_S + REPLAY_WATCH_S
        await asyncio.sleep(max(0.0, listen_end - time.time()))

        paused = [
            f"{recorded.event.event} {recorded.time - resumed:.1f} s after the seek"
            for recorded in events.events
            if recorded.time >= resumed and _paused(recorded.event, player_id)
        ]
        assert not paused, f"queue or player paused instead of ending: {paused}"

        received = capture(role)
        assert received.ended is None, f"{role} capture ended at {received.ended}"
        timeline = received.timeline()
        tail = silences(timeline, resumed, expected_end - END_TOLERANCE_S)
        assert max((seg.duration for seg in tail), default=0.0) < MAX_TRACK_SILENCE_S, (
            f"the track went silent before its end ({expected_end:.1f}): {tail}"
        )
        replay = [
            seg
            for seg in timeline
            if seg.kind != "silence" and seg.t1 > expected_end + REPLAY_AFTER_S
        ]
        assert not replay, f"sound after the track ended ({expected_end:.1f}): {replay}"

        errors = ma_log_errors(log_offset)
        assert not errors, f"server log errors: {errors}"
    finally:
        await client.player_queues.stop(player_id)


def _paused(event: MassEvent, player_id: str) -> bool:
    """Whether an event shows the queue or the player of `player_id` paused."""
    from music_assistant_models.enums import EventType

    if event.object_id != player_id:
        return False
    if event.event == EventType.QUEUE_UPDATED:
        return bool(event.data["state"] == "paused")
    if event.event == EventType.PLAYER_UPDATED:
        return bool(event.data.get("playback_state") == "paused")
    return False
