"""Gate area library: the scan follows the music folder and rows survive a server restart."""

from __future__ import annotations

import time

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.library import rows_survive_restart, scan_matches_manifest

# the restart runs last: it leaves the test's own client disconnected
pytestmark = [
    pytest.mark.gate,
    pytest.mark.area_library,
    pytest.mark.roles("slim-1"),
    pytest.mark.timeout(600),
]


async def test_library(role: str, ctx: CheckContext) -> None:
    """The library follows files added and deleted, and keeps its rows over a restart."""
    started = time.time()
    await scan_matches_manifest(ctx)
    await rows_survive_restart(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
