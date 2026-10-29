"""Track boundary continuity on a real streaming account: the queue plays into item 2 (#6860)."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.music import continuity

pytestmark = [pytest.mark.music, pytest.mark.timeout(180)]


async def test_continuity(role: str, ctx: CheckContext) -> None:
    """Item 1 plays to its end and the queue moves on to item 2 without stopping in between."""
    await continuity(ctx, role)
