"""A pause from the Spotify side stops the queue and keeps the resume position (#6752, #7117)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from scenarios.helpers import (
    describe,
    pick_tracks,
    queue_field,
    queue_playing,
    soloist_command,
)

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient

    from scenarios.conftest import EventRecorder

pytestmark = [pytest.mark.music, pytest.mark.timeout(180)]

START_TIMEOUT_S = 30.0
PAUSE_AFTER_S = 10.0
STOP_WITHIN_S = 3.0
IDLE_TIMEOUT_S = 10.0
# the resume position may trail the elapsed time at the pause by what the player had buffered
RESUME_TOLERANCE_S = 3.0


async def test_external_pause(
    role: str,
    client: MusicAssistantClient,
    players: dict[str, str],
    events: EventRecorder,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    """Pausing the Soloist engine mid-track stops the queue with a resume position."""
    from music_assistant_models.enums import EventType, QueueOption

    spotify = [p for p in client.providers if p.domain == "spotify"]
    if not spotify:
        pytest.skip("needs a Spotify account set (Soloist backend); this golden has no spotify")
    player_id = players[role]
    [track] = await pick_tracks(client, 1)
    record_testsuite_property(f"external_pause[{role}] track", describe(track))

    started = time.time()
    try:
        await client.player_queues.play_media(player_id, track, QueueOption.REPLACE)
        await events.wait_for(
            EventType.QUEUE_UPDATED, queue_playing(player_id, 0), START_TIMEOUT_S, since=started
        )
        await asyncio.sleep(PAUSE_AFTER_S)
        queue = client.player_queues.get(player_id)
        assert queue is not None
        elapsed = queue.corrected_elapsed_time
        paused = time.time()
        await soloist_command(spotify[0].instance_id, "pause")
        try:
            # a stop sets the resume position at once; idle follows when the player confirms
            await events.wait_for(
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
        record_testsuite_property(f"external_pause[{role}] stop s", f"{time.time() - paused:.2f}")
        stopped = await events.wait_for(
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
        await client.player_queues.stop(player_id)
