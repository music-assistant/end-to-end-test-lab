"""Gate area bridge: a squeezelite player in a Sendspin group, through its Sendspin bridge."""

from __future__ import annotations

import time

import pytest

from scenarios.checks.bridge import (
    bridge_member_joins_and_leaves,
    bridge_output_listed,
    mixed_group_plays_in_sync,
    mixed_group_stops_and_ends,
    squeezelite_group_plays_over_sendspin,
)
from scenarios.checks.context import CheckContext

# the area boots slim-1, slim-2 and sendspin-1; the journey runs once on slim-1, grouped with
# sendspin-1 and with slim-2
pytestmark = [
    pytest.mark.gate,
    pytest.mark.area_bridge,
    pytest.mark.roles("slim-1"),
    pytest.mark.timeout(600),
]


async def test_bridge(role: str, ctx: CheckContext) -> None:
    """The role plays in a Sendspin group in sync, joins and leaves one, and natively on its own."""
    started = time.time()
    await bridge_output_listed(ctx, role)
    await mixed_group_plays_in_sync(ctx, role)
    await bridge_member_joins_and_leaves(ctx, role)
    await mixed_group_stops_and_ends(ctx, role)
    await squeezelite_group_plays_over_sendspin(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
