"""Gate area streams: flow, crossfade, seek, elapsed time and short files; radio on slim only."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

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
    pytest.mark.gate,
    pytest.mark.area_streams,
    pytest.mark.roles(*AREA_ROLES["streams"]),
    pytest.mark.timeout(600),
]

Check = Callable[[CheckContext, str], Awaitable[None]]

PLAYBACK_CHECKS: tuple[Check, ...] = (
    flow_continuous,
    flow_next_skips_one,
    crossfade_overlap,
    seek_resumes,
    elapsed_tracks_pause,
    stop_after_end_position,
    short_file_plays,
)
RADIO_CHECKS: tuple[Check, ...] = (
    radio_start_latency,
    radio_icy_plays,
    radio_hls_plays,
    radio_403_rejected,
)
# radio runs on slim only, to keep the gate within its time limit
JOURNEYS: dict[str, tuple[Check, ...]] = {
    "slim-1": (*PLAYBACK_CHECKS, *RADIO_CHECKS),
    "sendspin-1": PLAYBACK_CHECKS,
}


async def test_streams(role: str, ctx: CheckContext) -> None:
    """The role streams flow mode, crossfades, seeks and tracks elapsed time; slim plays radio."""
    started = time.time()
    for check in JOURNEYS[role]:
        await check(ctx, role)
    ctx.record_property("journey_s", round(time.time() - started, 1))
