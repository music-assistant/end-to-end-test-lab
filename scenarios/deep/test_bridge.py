"""Deep area bridge: each Sendspin bridge check on its own, on the squeezelite role."""

from __future__ import annotations

import pytest

from scenarios.checks.bridge import (
    bridge_member_joins_and_leaves,
    bridge_output_listed,
    mixed_group_plays_in_sync,
    mixed_group_stops_and_ends,
    squeezelite_group_stays_native,
)
from scenarios.checks.context import CheckContext

# every check groups the role with sendspin-1, the last one with slim-2
pytestmark = [
    pytest.mark.deep,
    pytest.mark.area_bridge,
    pytest.mark.roles("slim-1"),
    pytest.mark.timeout(300),
]


async def test_bridge_output_listed(role: str, ctx: CheckContext) -> None:
    """A squeezelite player lists a Sendspin output and stays one player."""
    await bridge_output_listed(ctx, role)


async def test_mixed_group_plays_in_sync(role: str, ctx: CheckContext) -> None:
    """A squeezelite player plays in a Sendspin group, over Sendspin and in sync."""
    await mixed_group_plays_in_sync(ctx, role)


async def test_bridge_member_joins_and_leaves(role: str, ctx: CheckContext) -> None:
    """A squeezelite player joins a playing Sendspin player in sync and leaves it alone."""
    await bridge_member_joins_and_leaves(ctx, role)


async def test_mixed_group_stops_and_ends(role: str, ctx: CheckContext) -> None:
    """A stop silences a bridged squeezelite player at once and a queue plays to its end."""
    await mixed_group_stops_and_ends(ctx, role)


async def test_squeezelite_group_stays_native(role: str, ctx: CheckContext) -> None:
    """A group of squeezelite players and a player on its own keep their own protocol."""
    await squeezelite_group_stays_native(ctx, role)
