"""Gate area queue: album, pause and resume, and now playing on every queue role."""

from __future__ import annotations

import time

import pytest

from labctl.areas import AREA_ROLES
from scenarios.checks.context import CheckContext
from scenarios.checks.queue import album_plays_through, now_playing_follows, resume_advances

pytestmark = [
    pytest.mark.gate,
    pytest.mark.area_queue,
    pytest.mark.roles(*AREA_ROLES["queue"]),
    pytest.mark.timeout(600),
]


async def test_queue(role: str, ctx: CheckContext) -> None:
    """The role plays an album through, resumes and advances, and reports now playing in time."""
    started = time.time()
    await album_plays_through(ctx, role)
    await resume_advances(ctx, role)
    await now_playing_follows(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
