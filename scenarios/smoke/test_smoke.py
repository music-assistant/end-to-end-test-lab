"""Lab smoke test: every player role plays a tone and goes silent on stop."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.smoke import play_stop_heard

pytestmark = [pytest.mark.smoke, pytest.mark.timeout(120)]


async def test_smoke(role: str, ctx: CheckContext) -> None:
    """Tone track A reaches the player's capture after play and stops after stop."""
    await play_stop_heard(ctx, role)
