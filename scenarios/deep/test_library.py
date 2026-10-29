"""Deep area library: each library check on its own."""

from __future__ import annotations

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.library import rows_survive_restart, scan_matches_manifest

pytestmark = [
    pytest.mark.deep,
    pytest.mark.area_library,
    pytest.mark.roles("slim-1"),
    pytest.mark.timeout(300),
]


async def test_scan_matches_manifest(role: str, ctx: CheckContext) -> None:
    """The library matches the tone library and follows a file added and deleted."""
    await scan_matches_manifest(ctx)


async def test_rows_survive_restart(role: str, ctx: CheckContext) -> None:
    """A favorite and a play log entry survive a server restart."""
    await rows_survive_restart(ctx, role)
