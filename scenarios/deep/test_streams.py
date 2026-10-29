"""Deep area streams: each stream check on its own, on every streams role."""

from __future__ import annotations

import pytest

from labctl.areas import AREA_ROLES
from scenarios.checks.context import CheckContext
from scenarios.checks.streams import (
    crossfade_overlap,
    elapsed_tracks_pause,
    flow_continuous,
    flow_next_skips_one,
    radio_403_rejected,
    radio_hls_plays,
    radio_icy_plays,
    radio_start_latency,
    seek_resumes,
    short_file_plays,
    stop_after_end_position,
)

pytestmark = [
    pytest.mark.deep,
    pytest.mark.area_streams,
    pytest.mark.roles(*AREA_ROLES["streams"]),
]


async def test_flow_continuous(role: str, ctx: CheckContext) -> None:
    """An album plays as one gapless flow stream and ends for good."""
    await flow_continuous(ctx, role)


async def test_flow_next_skips_one(role: str, ctx: CheckContext) -> None:
    """Next in flow mode moves exactly one track ahead."""
    await flow_next_skips_one(ctx, role)


async def test_crossfade_overlap(role: str, ctx: CheckContext) -> None:
    """Two tracks crossfade with no gap and no skip."""
    await crossfade_overlap(ctx, role)


async def test_seek_resumes(role: str, ctx: CheckContext) -> None:
    """A seek reports the new position and plays on from there."""
    await seek_resumes(ctx, role)


# pause and stop issues come mostly from real devices; Snapcast is the closest virtual stand-in
@pytest.mark.roles(*AREA_ROLES["streams"], "snap-1")
async def test_elapsed_tracks_pause(role: str, ctx: CheckContext) -> None:
    """Elapsed time stops while paused and resets for the next track."""
    await elapsed_tracks_pause(ctx, role)


@pytest.mark.roles(*AREA_ROLES["streams"], "snap-1")
async def test_stop_after_end_position(role: str, ctx: CheckContext) -> None:
    """A track played to its end replays from the start."""
    await stop_after_end_position(ctx, role)


async def test_short_file_plays(role: str, ctx: CheckContext) -> None:
    """A 1.5 s track is heard and the queue advances past it."""
    await short_file_plays(ctx, role)


async def test_radio_start_latency(role: str, ctx: CheckContext) -> None:
    """An ICY radio stream is heard within 4 s."""
    await radio_start_latency(ctx, role)


async def test_radio_icy_plays(role: str, ctx: CheckContext) -> None:
    """An ICY stream plays, follows its titles and survives a drop."""
    await radio_icy_plays(ctx, role)


async def test_radio_hls_plays(role: str, ctx: CheckContext) -> None:
    """A live HLS stream plays without stalling."""
    await radio_hls_plays(ctx, role)


async def test_radio_403_rejected(role: str, ctx: CheckContext) -> None:
    """A stream that answers 403 is refused at once and the player stays silent."""
    await radio_403_rejected(ctx, role)
