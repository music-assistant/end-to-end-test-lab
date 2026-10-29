"""Deep area core: the server boots clean."""

from __future__ import annotations

import pytest

from labctl.areas import AREA_ROLES
from scenarios.checks.context import CheckContext
from scenarios.checks.health import boot_clean

pytestmark = [
    pytest.mark.deep,
    pytest.mark.area_core,
    pytest.mark.roles(*AREA_ROLES["core"]),
    pytest.mark.timeout(300),
]


async def test_boot_clean(role: str, ctx: CheckContext) -> None:
    """Providers load, roles register and nothing is logged as an error."""
    # `role` only selects the player the `players` fixture waits for
    await boot_clean(ctx)
