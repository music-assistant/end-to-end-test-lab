"""Device state checks on every leased device: volume, mute, long pause, stop, radio, restart."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING

import pytest

from scenarios.checks.bench import (
    bench_group_state,
    bench_long_pause,
    bench_mute,
    bench_radio_starts,
    bench_restart_rediscovers,
    bench_stop_then_play,
    bench_volume_kept,
    bench_volume_reported,
)
from scenarios.checks.containers import PROJECT_ENV
from scenarios.checks.context import CheckContext

if TYPE_CHECKING:
    from scenarios.conftest import LabRun

pytestmark = [pytest.mark.bench, pytest.mark.timeout(300)]

needs_docker = pytest.mark.skipif(
    PROJECT_ENV not in os.environ, reason="no docker engine access in this run"
)


async def test_volume_reported(device: str, ctx: CheckContext) -> None:
    """The device reports volume 3 and then 1 when set while it plays."""
    await _timed(ctx, device, bench_volume_reported(ctx, device))


async def test_mute(device: str, ctx: CheckContext) -> None:
    """The device reports mute and unmute, and unmute keeps its volume."""
    await _timed(ctx, device, bench_mute(ctx, device))


async def test_volume_kept(device: str, ctx: CheckContext) -> None:
    """The device keeps its volume through play, pause, resume and next."""
    await _timed(ctx, device, bench_volume_kept(ctx, device))


async def test_long_pause(device: str, ctx: CheckContext) -> None:
    """The server stops a 30 s pause, the queue is idle at 60 s and resume plays on in place."""
    await _timed(ctx, device, bench_long_pause(ctx, device))


async def test_stop_then_play(device: str, ctx: CheckContext) -> None:
    """Stop mid-track goes idle and the next play starts from the beginning."""
    await _timed(ctx, device, bench_stop_then_play(ctx, device))


async def test_radio_starts(device: str, ctx: CheckContext) -> None:
    """The lab radio's ICY stream starts and keeps playing."""
    await _timed(ctx, device, bench_radio_starts(ctx, device))


@pytest.mark.devices("sonos")
async def test_sonos_group(device: str, ctx: CheckContext, run: LabRun) -> None:
    """Two Sonos group, play on both, keep the group on next and ungroup back to the leader."""
    sonos = sorted(d for d, dev in run.devices.items() if dev.provider == "sonos")
    if device != sonos[0]:
        pytest.skip("leader only")
    if len(sonos) < 2:
        pytest.skip("one sonos leased")
    await _timed(ctx, device, bench_group_state(ctx, device, sonos[1]))


@needs_docker
async def test_restart_rediscovers(device: str, ctx: CheckContext, run: LabRun) -> None:
    """Every leased device is available again after a server restart and the first one plays."""
    # parametrized over the devices so the players fixture waits for one, but runs only once
    if device != min(run.devices):
        pytest.skip("runs once")
    await _timed(ctx, device, bench_restart_rediscovers(ctx, run.devices))


async def _timed(ctx: CheckContext, device: str, check: Awaitable[None]) -> None:
    """Run the check; records the device and the check's time."""
    ctx.record_property("device", device)
    started = time.time()
    await check
    ctx.record_property("check_s", round(time.time() - started, 1))
