"""Lab smoke test: every player role plays a tone and goes silent on stop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from labctl.analyze import Capture, Manifest
from scenarios.helpers import until

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient

pytestmark = [pytest.mark.smoke, pytest.mark.timeout(120)]

PLAYING_TIMEOUT_S = 10.0
TONE_WITHIN_S = 10.0
# enough tone to be sure it is the track and not a stray window
TONE_MIN_S = 1.0
SILENT_WITHIN_S = 2.0
# silence observed past the deadline before the capture counts as silent
SILENT_MIN_S = 1.0
POLL_S = 0.5


async def test_smoke(
    role: str,
    client: MusicAssistantClient,
    players: dict[str, str],
    tones: Manifest,
    capture: Callable[[str], Capture],
) -> None:
    """Tone track A reaches the player's capture after play and stops after stop."""
    from music_assistant_models.enums import PlaybackState

    player_id = players[role]
    tone = tones.tracks[0]
    [track] = await client.music.get_library_tracks(search=tone.title, limit=1)

    played = time.time()
    await client.player_queues.play_media(player_id, track)

    def playing() -> bool:
        player = client.players.get(player_id)
        return player is not None and player.playback_state == PlaybackState.PLAYING

    await until(playing, PLAYING_TIMEOUT_S, POLL_S)

    def tone_heard() -> bool:
        return any(
            seg.kind == tone.id and seg.t0 <= played + TONE_WITHIN_S and seg.duration >= TONE_MIN_S
            for seg in capture(role).timeline()
        )

    await until(tone_heard, TONE_WITHIN_S + TONE_MIN_S + POLL_S, POLL_S)

    stopped = time.time()
    await client.players.stop(player_id)
    await asyncio.sleep(SILENT_WITHIN_S + SILENT_MIN_S)
    after_stop = capture(role)
    # unwritten time counts as silence, so a dead capture sidecar would pass the check below
    assert after_stop.ended is None, f"{role} capture ended at {after_stop.ended}, stop {stopped}"
    sound = [
        seg
        for seg in after_stop.timeline()
        if seg.kind != "silence" and seg.t1 > stopped + SILENT_WITHIN_S
    ]
    assert not sound, f"{role} still has sound {SILENT_WITHIN_S} s after stop: {sound}"
