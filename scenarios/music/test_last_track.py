"""The last queue item plays to its end and the queue stops cleanly (#6860, #6667, #6222)."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.music import last_track_ends_clean

pytestmark = [pytest.mark.music, pytest.mark.timeout(180)]


async def test_last_track_ends_clean(role: str, ctx: CheckContext) -> None:
    """A single queued track plays to its end, the queue goes idle and nothing plays after."""
    await last_track_ends_clean(ctx, role)
