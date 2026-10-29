"""Bench play and stop: every leased device plays a tone and goes idle on stop."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import EventType, QueueOption

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient
    from music_assistant_models.event import MassEvent

    from labctl.analyze import Manifest
    from scenarios.conftest import EventRecorder

pytestmark = [pytest.mark.bench, pytest.mark.timeout(300)]

TONE = "a1t4"
# real devices buffer and start slower than the virtual players
PLAYING_TIMEOUT_S = 20.0
IDLE_TIMEOUT_S = 10.0


async def test_play_stop(
    device: str,
    players: dict[str, str],
    client: MusicAssistantClient,
    events: EventRecorder,
    tones: Manifest,
    record_property: Callable[[str, object], None],
) -> None:
    """The device reports playing after play and idle after stop; records both latencies."""
    player_id = players[device]
    title = next(tone.title for tone in tones.tracks if tone.id == TONE)
    [track] = await client.music.get_library_tracks(search=title, limit=1)

    since = time.time()
    await client.player_queues.play_media(player_id, track, QueueOption.REPLACE)
    try:
        await events.wait_for(
            EventType.PLAYER_UPDATED, _state(player_id, "playing"), PLAYING_TIMEOUT_S, since
        )
        record_property("playing_s", round(time.time() - since, 2))
    finally:
        # a real speaker keeps playing after a failed check unless it is stopped
        since = time.time()
        await client.player_queues.stop(player_id)
    await events.wait_for(
        EventType.PLAYER_UPDATED, _state(player_id, "idle"), IDLE_TIMEOUT_S, since
    )
    record_property("idle_s", round(time.time() - since, 2))


def _state(player_id: str, state: str) -> Callable[[MassEvent], bool]:
    """Match a PLAYER_UPDATED event of `player_id` in playback state `state`."""

    def matching(event: MassEvent) -> bool:
        return event.object_id == player_id and event.data.get("playback_state") == state

    # names the state in the timeout message
    matching.__name__ = state
    return matching
