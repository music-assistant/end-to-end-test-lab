"""A pause from the Spotify side stops the queue and keeps the resume position (#6752, #7117)."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.music import external_pause

pytestmark = [pytest.mark.music, pytest.mark.timeout(180)]


async def test_external_pause(role: str, ctx: CheckContext) -> None:
    """Pausing the Soloist engine mid-track stops the queue with a resume position."""
    await external_pause(ctx, role)
