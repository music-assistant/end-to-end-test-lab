"""Gate area core: the server boots clean and plays an album through."""

from __future__ import annotations

import pytest

from labctl.areas import AREA_ROLES
from scenarios.checks.context import CheckContext
from scenarios.checks.health import boot_clean
from scenarios.checks.queue import album_plays_through

pytestmark = [pytest.mark.gate, pytest.mark.area_core, pytest.mark.roles(*AREA_ROLES["core"])]


async def test_core(role: str, ctx: CheckContext) -> None:
    """The server boots clean and the role plays an album through."""
    await boot_clean(ctx)
    await album_plays_through(ctx, role)
