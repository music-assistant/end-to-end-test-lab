"""Bench play and stop: every leased device plays a tone and goes idle on stop."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.smoke import play_stop_reported

pytestmark = [pytest.mark.bench, pytest.mark.timeout(300)]


async def test_play_stop(device: str, ctx: CheckContext) -> None:
    """The device reports playing after play and idle after stop; records both latencies."""
    await play_stop_reported(ctx, device)
