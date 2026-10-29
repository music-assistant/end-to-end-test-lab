"""Bench journey: the same checks on every leased real device, state and position only."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import pytest

from scenarios.checks.bench import (
    bench_album,
    bench_group_skip,
    bench_next_skips_one,
    bench_pause_resume,
    bench_seek,
    bench_stop_position,
)
from scenarios.checks.context import CheckContext

if TYPE_CHECKING:
    from scenarios.conftest import LabRun

pytestmark = [pytest.mark.hw, pytest.mark.tier1]

Check = Callable[[CheckContext, str], Awaitable[None]]

JOURNEY: tuple[Check, ...] = (
    bench_album,
    bench_pause_resume,
    bench_seek,
    bench_stop_position,
    bench_next_skips_one,
)


async def test_bench(device: str, ctx: CheckContext, run: LabRun) -> None:
    """The device plays the album, pauses, seeks, stops at its end and skips; pairs group skip."""
    checks = list(JOURNEY)
    provider = run.devices[device].provider
    siblings = sorted(
        d for d, dev in run.devices.items() if dev.provider == provider and d != device
    )
    if siblings and device < siblings[0]:
        checks.append(functools.partial(bench_group_skip, second=siblings[0]))
    ctx.record_property("device", device)
    started = time.time()
    for check in checks:
        await check(ctx, device)
    ctx.record_property("journey_s", round(time.time() - started, 1))
