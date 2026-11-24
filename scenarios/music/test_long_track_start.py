"""A long track starts as fast as a short one: no start cost growing with its length (#7205)."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.music import long_track_starts_fast

pytestmark = [pytest.mark.music, pytest.mark.timeout(90)]


async def test_long_track_starts_fast(role: str, ctx: CheckContext) -> None:
    """A track of over 10 minutes is heard within a few seconds of play from a stopped queue."""
    await long_track_starts_fast(ctx, role)
