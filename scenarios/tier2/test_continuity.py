"""Track boundary continuity on a real streaming account: the queue plays into item 2 (#6860)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from labctl.analyze import Capture
from scenarios.helpers import (
    describe,
    ma_log_since,
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

pytestmark = pytest.mark.tier2

# the seek leaves this much of track 1, so the test sees its natural end quickly
TAIL_S = 20
START_TIMEOUT_S = 30.0
# track 1 plays this long before the seek, so the seek does not race the start
PLAYED_BEFORE_SEEK_S = 3.0
SEEK_TIMEOUT_S = 15.0
ADVANCE_MARGIN_S = 15.0
TRACK2_LISTEN_S = 8.0
# reported duration, seek and elapsed time are whole seconds and a fade out can dip below -60 dB
END_TOLERANCE_S = 3.0
MAX_TRACK_SILENCE_S = 1.0
MAX_BOUNDARY_GAP_S = 3.0
STOPPED_STATES = ("idle", "paused")


async def test_continuity(
    role: str,
    client: MusicAssistantClient,
    players: dict[str, str],
    events: EventRecorder,
    capture: Callable[[str], Capture],
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    """Item 1 plays to its end and the queue moves on to item 2 without stopping in between."""
    from music_assistant_models.enums import EventType, QueueOption

    player_id = players[role]
    first, second = await pick_tracks(client, 2)
    record_testsuite_property(
        f"continuity[{role}] tracks", f"{describe(first)} then {describe(second)}"
    )
    assert first.duration is not None
    log_offset = ma_log_size()

    def time_from(minimum: float) -> Callable[[MassEvent], bool]:
        def reached(event: MassEvent) -> bool:
            return event.object_id == player_id and float(event.data) >= minimum

        return reached

    started = time.time()
    try:
        await client.player_queues.play_media(player_id, [first, second], QueueOption.REPLACE)
        await events.wait_for(
            EventType.QUEUE_UPDATED, queue_playing(player_id, 0), START_TIMEOUT_S, since=started
        )
        await asyncio.sleep(PLAYED_BEFORE_SEEK_S)

        position = first.duration - TAIL_S
        seeked = time.time()
        await client.player_queues.seek(player_id, position)
        at_seek = await events.wait_for(
            EventType.QUEUE_TIME_UPDATED, time_from(position - 1), SEEK_TIMEOUT_S, since=seeked
        )
        resumed = time.time()
        expected_end = resumed + first.duration - float(at_seek.data)

        update = await events.wait_for(
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
            for recorded in events.events
            if resumed <= recorded.time <= advanced and _stopped(recorded.event, player_id)
        ]
        assert not stops, f"queue or player stopped before item 2: {stops}"

        listen_end = max(advanced, expected_end) + TRACK2_LISTEN_S
        await asyncio.sleep(listen_end - time.time())
        received = capture(role)
        assert received.ended is None, f"{role} capture ended at {received.ended}"
        timeline = received.timeline()
        tail = silences(timeline, resumed, expected_end - END_TOLERANCE_S)
        assert max((seg.duration for seg in tail), default=0.0) < MAX_TRACK_SILENCE_S, (
            f"item 1 went silent before its end ({expected_end:.1f}): {tail}"
        )
        boundary = silences(timeline, expected_end - END_TOLERANCE_S, listen_end)
        gap = max((seg.duration for seg in boundary), default=0.0)
        record_testsuite_property(f"continuity[{role}] boundary gap s", f"{gap:.2f}")
        assert gap < MAX_BOUNDARY_GAP_S, (
            f"{gap:.1f} s gap between item 1 and item 2 (end {expected_end:.1f}): {boundary}"
        )

        stop_lines = [line for line in ma_log_since(log_offset) if "stopping queue" in line]
        assert not stop_lines, f"server log: {stop_lines}"
    finally:
        await client.player_queues.stop(player_id)


def _stopped(event: MassEvent, player_id: str) -> bool:
    """Whether an event shows the queue or the player of `player_id` idle or paused."""
    from music_assistant_models.enums import EventType

    if event.object_id != player_id:
        return False
    if event.event == EventType.QUEUE_UPDATED:
        return bool(event.data["state"] in STOPPED_STATES)
    if event.event == EventType.PLAYER_UPDATED:
        return bool(event.data.get("playback_state") in STOPPED_STATES)
    return False
