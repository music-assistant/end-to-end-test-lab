"""Deep area queue: each queue check on its own, on every queue role."""

from __future__ import annotations

import pytest

from labctl.areas import AREA_ROLES
from scenarios.checks.context import CheckContext
from scenarios.checks.queue import (
    album_plays_through,
    next_drops_previous,
    now_playing_follows,
    resume_advances,
)

pytestmark = [
    pytest.mark.deep,
    pytest.mark.area_queue,
    pytest.mark.roles(*AREA_ROLES["queue"]),
    pytest.mark.timeout(300),
]


async def test_album_plays_through(role: str, ctx: CheckContext) -> None:
    """Three album tracks play in order to the end of the queue."""
    await album_plays_through(ctx, role)


async def test_resume_advances(role: str, ctx: CheckContext) -> None:
    """A paused track resumes and still advances to the next item."""
    await resume_advances(ctx, role)


async def test_next_drops_previous(role: str, ctx: CheckContext) -> None:
    """Next mid-track silences the skipped track and plays the next within 3 s."""
    await next_drops_previous(ctx, role)


async def test_now_playing_follows(role: str, ctx: CheckContext) -> None:
    """Now playing follows the audio to track 2."""
    await now_playing_follows(ctx, role)
