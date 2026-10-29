"""Player provider journeys: one queue, volume and announcement journey per lab player kind."""

from __future__ import annotations

import time

import pytest

from scenarios.checks.context import CheckContext
from scenarios.checks.players import announce_idle_only_clip, volume_set_audible
from scenarios.checks.queue import album_plays_through, next_drops_previous, resume_advances

pytestmark = [pytest.mark.gate, pytest.mark.timeout(600)]


@pytest.mark.provider_sendspin
@pytest.mark.roles("sendspin-1")
async def test_player_sendspin(role: str, ctx: CheckContext) -> None:
    """A Sendspin player plays an album, resumes, skips and announces."""
    await _journey(ctx, role)


@pytest.mark.provider_airplay
@pytest.mark.roles("airplay-1")
async def test_player_airplay(role: str, ctx: CheckContext) -> None:
    """An AirPlay player plays an album, resumes, skips, sets volume and announces."""
    await _journey(ctx, role)


@pytest.mark.provider_dlna
@pytest.mark.roles("dlna-1")
async def test_player_dlna(role: str, ctx: CheckContext) -> None:
    """A DLNA player plays an album, resumes, skips, sets volume and announces."""
    await _journey(ctx, role)


@pytest.mark.provider_squeezelite
@pytest.mark.roles("slim-1")
async def test_player_slim(role: str, ctx: CheckContext) -> None:
    """A squeezelite player plays an album, resumes, skips, sets volume and announces."""
    await _journey(ctx, role)


@pytest.mark.provider_snapcast
@pytest.mark.roles("snap-1")
async def test_player_snap(role: str, ctx: CheckContext) -> None:
    """A Snapcast player plays an album, resumes, skips, sets volume and announces."""
    await _journey(ctx, role)


@pytest.mark.provider_mpd
@pytest.mark.roles("mpd-1")
async def test_player_mpd(role: str, ctx: CheckContext) -> None:
    """An MPD player plays an album, resumes, skips, sets volume and announces."""
    await _journey(ctx, role)


async def _journey(ctx: CheckContext, role: str) -> None:
    """Album, resume, next, volume where the player advertises it, idle announcement."""
    from music_assistant_models.enums import PlayerFeature

    started = time.time()
    await album_plays_through(ctx, role)
    await resume_advances(ctx, role)
    await next_drops_previous(ctx, role)
    player = ctx.client.players.get(ctx.player_id(role))
    volume = player is not None and PlayerFeature.VOLUME_SET in player.supported_features
    ctx.record_property("volume_checked", volume)
    if volume:
        await volume_set_audible(ctx, role)
    await announce_idle_only_clip(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
