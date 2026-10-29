"""Deep area players: each player check on its own, on the roles that support it."""

from __future__ import annotations

import os

import pytest

from scenarios.checks.containers import PROJECT_ENV
from scenarios.checks.context import CheckContext
from scenarios.checks.players import (
    announce_ducks_and_resumes,
    announce_idle_only_clip,
    announce_short_and_back_to_back,
    disconnect_closes_stream,
    group_announce_restores,
    group_plays_in_sync,
    group_skip_intact,
    group_volume_average,
    member_rejoins,
    member_removed_keeps_leader,
    member_restart_ungroups,
    muted_member_stays_muted,
    pause_holds,
    volume_after_pause,
    volume_set_audible,
)

pytestmark = [pytest.mark.deep, pytest.mark.area_players, pytest.mark.timeout(300)]

# group checks take the role's next index of the same kind as partner
GROUP_ROLES = ("slim-1", "snap-1", "sendspin-1")
# the lab Sendspin client has no volume control
VOLUME_ROLES = ("slim-1", "snap-1")
needs_docker = pytest.mark.skipif(
    PROJECT_ENV not in os.environ, reason="no docker engine access outside a tier 1 run"
)


@pytest.mark.roles(*GROUP_ROLES)
async def test_group_plays_in_sync(role: str, ctx: CheckContext) -> None:
    """A two-player group plays in sync and advances on both."""
    await group_plays_in_sync(ctx, role)


@pytest.mark.roles(*GROUP_ROLES)
async def test_member_removed_keeps_leader(role: str, ctx: CheckContext) -> None:
    """Ungrouping a member leaves the leader playing and the member usable."""
    await member_removed_keeps_leader(ctx, role)


@pytest.mark.roles(*GROUP_ROLES)
async def test_group_skip_intact(role: str, ctx: CheckContext) -> None:
    """Next on a group skips one track on both and keeps the group."""
    await group_skip_intact(ctx, role)


@pytest.mark.roles(*GROUP_ROLES)
async def test_group_announce_restores(role: str, ctx: CheckContext) -> None:
    """An announcement on a member plays and the member rejoins the group."""
    await group_announce_restores(ctx, role)


@needs_docker
@pytest.mark.roles(*GROUP_ROLES)
async def test_member_rejoins(role: str, ctx: CheckContext) -> None:
    """A restarted static syncgroup member drops out, returns and rejoins the playing group."""
    await member_rejoins(ctx, role)


@needs_docker
@pytest.mark.roles(*GROUP_ROLES)
async def test_member_restart_ungroups(role: str, ctx: CheckContext) -> None:
    """A restarted native group member leaves the group and the leader plays on."""
    await member_restart_ungroups(ctx, role)


@pytest.mark.roles(*VOLUME_ROLES)
async def test_muted_member_stays_muted(role: str, ctx: CheckContext) -> None:
    """A group volume change keeps a muted member muted."""
    await muted_member_stays_muted(ctx, role)


@pytest.mark.roles(*VOLUME_ROLES)
async def test_volume_set_audible(role: str, ctx: CheckContext) -> None:
    """A lower volume is heard quieter and full volume restores the level."""
    await volume_set_audible(ctx, role)


@pytest.mark.roles(*VOLUME_ROLES)
async def test_group_volume_average(role: str, ctx: CheckContext) -> None:
    """The group volume is the members' average and can be set."""
    await group_volume_average(ctx, role)


@pytest.mark.roles(*VOLUME_ROLES)
async def test_volume_after_pause(role: str, ctx: CheckContext) -> None:
    """The volume stays put through pause, resume and next."""
    await volume_after_pause(ctx, role)


@pytest.mark.roles("slim-1", "snap-1", "sendspin-1")
async def test_pause_holds(role: str, ctx: CheckContext) -> None:
    """A pause holds for 20 s and resumes where it paused."""
    await pause_holds(ctx, role)


@needs_docker
@pytest.mark.roles(*GROUP_ROLES)
async def test_disconnect_closes_stream(role: str, ctx: CheckContext) -> None:
    """A stopped player goes unavailable and returns; its queue stops if MA streams to it."""
    await disconnect_closes_stream(ctx, role)


@pytest.mark.roles(*GROUP_ROLES)
async def test_announce_idle_only_clip(role: str, ctx: CheckContext) -> None:
    """An announcement on an idle player plays only the clip."""
    await announce_idle_only_clip(ctx, role)


@pytest.mark.roles(*GROUP_ROLES)
async def test_announce_short_and_back_to_back(role: str, ctx: CheckContext) -> None:
    """A 1.5 s clip plays whole, twice in a row and after the chime."""
    await announce_short_and_back_to_back(ctx, role)


@pytest.mark.roles(*VOLUME_ROLES)
async def test_announce_ducks_and_resumes(role: str, ctx: CheckContext) -> None:
    """An announcement over music plays and the music resumes at its volume and place."""
    await announce_ducks_and_resumes(ctx, role)
