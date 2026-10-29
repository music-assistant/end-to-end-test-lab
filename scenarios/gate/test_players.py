"""Gate area players: sync groups, volume, pause and announcements on a slim pair."""

from __future__ import annotations

import time

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.players import (
    announce_ducks_and_resumes,
    announce_idle_only_clip,
    announce_short_and_back_to_back,
    group_announce_restores,
    group_plays_in_sync,
    group_skip_intact,
    group_volume_average,
    member_removed_keeps_leader,
    pause_holds,
    volume_after_pause,
    volume_set_audible,
)

# the area boots slim-1 and slim-2; the journey runs once on slim-1 with slim-2 as its partner
pytestmark = [
    pytest.mark.gate,
    pytest.mark.area_players,
    pytest.mark.roles("slim-1"),
    pytest.mark.timeout(600),
]


async def test_players(role: str, ctx: CheckContext) -> None:
    """The role groups with its partner, sets volume, pauses and plays announcements."""
    started = time.time()
    await group_plays_in_sync(ctx, role)
    await group_skip_intact(ctx, role)
    await member_removed_keeps_leader(ctx, role)
    await group_announce_restores(ctx, role)
    await group_volume_average(ctx, role)
    await volume_set_audible(ctx, role)
    await volume_after_pause(ctx, role)
    await pause_holds(ctx, role)
    await announce_ducks_and_resumes(ctx, role)
    await announce_idle_only_clip(ctx, role)
    await announce_short_and_back_to_back(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
