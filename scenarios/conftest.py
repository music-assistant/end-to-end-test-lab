"""Fixtures for scenarios running in the tests container against one lab run."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from labctl.analyze import Capture, Manifest
from labctl.areas import account_set_area
from labctl.devices import Device, from_env, matches
from scenarios.checks.context import CaptureLoader, CheckContext

if TYPE_CHECKING:
    # the client library only exists in the tests container
    from music_assistant_client import MusicAssistantClient
    from music_assistant_models.enums import EventType
    from music_assistant_models.event import MassEvent

CONNECT_TIMEOUT_S = 30.0
PLAYERS_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class LabRun:
    """The run this test session belongs to, as set up by the run lifecycle."""

    run_id: str
    ma_url: str
    token: str
    roles: tuple[str, ...]
    captures_dir: Path
    tones_manifest: Path
    # leased real devices by registry id
    devices: dict[str, Device]


@dataclass(frozen=True)
class RecordedEvent:
    """A server event with the wall-clock time it arrived."""

    time: float
    event: MassEvent


class EventRecorder:
    """Stores every server event of a test and lets the test wait for one."""

    def __init__(self) -> None:
        """Start with an empty history."""
        self.events: list[RecordedEvent] = []
        self._changed = asyncio.Event()

    def record(self, event: MassEvent) -> None:
        """Client subscription callback."""
        self.events.append(RecordedEvent(time.time(), event))
        self._changed.set()

    async def wait_for(
        self,
        event_type: EventType,
        predicate: Callable[[MassEvent], bool] | None = None,
        timeout: float = 10.0,
        since: float | None = None,
    ) -> MassEvent:
        """Return the first matching event recorded at or after `since` (default: now).

        Pass a `time.time()` taken before the triggering command as `since`: events that
        arrive before the command returns are otherwise missed. Raises AssertionError when no
        matching event arrives within `timeout` seconds.
        """
        since = time.time() if since is None else since
        checked = 0
        try:
            async with asyncio.timeout(timeout):
                while True:
                    for recorded in self.events[checked:]:
                        if (
                            recorded.time >= since
                            and recorded.event.event == event_type
                            and (predicate is None or predicate(recorded.event))
                        ):
                            return recorded.event
                    checked = len(self.events)
                    self._changed.clear()
                    await self._changed.wait()
        except TimeoutError:
            matching = getattr(predicate, "__name__", "any")
            raise AssertionError(
                f"no {event_type.value} event matching {matching} within {timeout:.0f} s"
            ) from None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize `role` by the `roles` marker and `device` by the `devices` marker."""
    if "device" in metafunc.fixturenames:
        _parametrize_device(metafunc)
    if "role" not in metafunc.fixturenames:
        return
    marker = metafunc.definition.get_closest_marker("roles")
    if marker is None:
        metafunc.parametrize("role", _roles())
        return
    run_roles = _roles()
    # roles the run did not boot still show up in the report, as skipped
    metafunc.parametrize(
        "role",
        [
            role
            if role in run_roles
            else pytest.param(role, marks=pytest.mark.skip(reason=f"role {role} not in this run"))
            for role in marker.args
        ],
    )


@pytest.fixture(scope="session")
def run() -> LabRun:
    """Return the lab run described by the LAB_* environment."""
    return lab_run()


@pytest.fixture(scope="session")
def tones(run: LabRun) -> Manifest:
    """Load the tone library manifest."""
    return Manifest.load(run.tones_manifest)


@pytest.fixture
def events() -> EventRecorder:
    """Record every server event of this test once the `client` fixture subscribes it."""
    return EventRecorder()


@pytest.fixture
async def client(run: LabRun, events: EventRecorder) -> AsyncIterator[MusicAssistantClient]:
    """Yield a connected, listening client with the initial server state loaded."""
    async with connect(run, events) as ma:
        yield ma


@pytest.fixture
async def players(
    run: LabRun, client: MusicAssistantClient, request: pytest.FixtureRequest
) -> dict[str, str]:
    """Map run roles and devices to player ids once the test's own role or device is available.

    Other roles and devices are included only if their player is available at that moment.
    """
    callspec = getattr(request.node, "callspec", None)
    params = callspec.params if callspec is not None else {}
    subject: str = params["role"] if "role" in params else params["device"]
    deadline = time.monotonic() + PLAYERS_TIMEOUT_S
    while True:
        available = [player for player in client.players if player.available]
        found = {
            role: p.player_id for role in run.roles for p in available if p.name == f"lab-{role}"
        }
        found |= {
            device_id: p.player_id
            for device_id, device in run.devices.items()
            for p in available
            if matches(device, p.name, p.provider, [o.protocol_domain for o in p.output_protocols])
        }
        if subject in found:
            return found
        if time.monotonic() > deadline:
            pytest.fail(f"player not available after {PLAYERS_TIMEOUT_S:.0f} s: {subject}")
        await asyncio.sleep(0.5)


@pytest.fixture
def capture(run: LabRun, tones: Manifest) -> CaptureLoader:
    """Load a role's capture from the test's start as written so far; reread for a fresh view."""
    # a full read grows with the run (about 2 s at 18 min) and blocks the event loop meanwhile
    started = time.time()

    def load(role: str, since: float | None = None) -> Capture:
        start = started if since is None else since
        return Capture.load(run.captures_dir, role, tones, since=start)

    return load


@pytest.fixture
def ctx(
    client: MusicAssistantClient,
    players: dict[str, str],
    tones: Manifest,
    capture: CaptureLoader,
    events: EventRecorder,
    record_property: Callable[[str, object], None],
) -> CheckContext:
    """Bundle this test's fixtures for the shared checks."""
    return CheckContext(client, players, tones, capture, events, record_property)


@pytest.fixture(autouse=True)
def record_area(
    request: pytest.FixtureRequest, record_property: Callable[[str, object], None]
) -> None:
    """Record the area of every `area_<name>` and `provider_<domain>` marker.

    A `provider_<domain>` marker records the domain; a music test records its account set area.
    """
    areas = [
        marker.name.removeprefix("area_").removeprefix("provider_")
        for marker in request.node.iter_markers()
        if marker.name.startswith(("area_", "provider_"))
    ]
    if not areas and (account_set := os.environ.get("LAB_ACCOUNT_SET")):
        areas = [account_set_area(account_set)]
    for area in areas:
        record_property("area", area)


def lab_run() -> LabRun:
    """Return the lab run described by the LAB_* environment."""
    return LabRun(
        run_id=os.environ["LAB_RUN_ID"],
        ma_url=os.environ["LAB_MA_URL"],
        token=os.environ["LAB_MA_TOKEN"],
        roles=_roles(),
        captures_dir=Path(os.environ["LAB_CAPTURES_DIR"]),
        tones_manifest=Path(os.environ["LAB_TONES_MANIFEST"]),
        devices=from_env(os.environ["LAB_DEVICES"]),
    )


@contextlib.asynccontextmanager
async def connect(run: LabRun, events: EventRecorder) -> AsyncIterator[MusicAssistantClient]:
    """Connect a listening client that records into `events`, with the server state loaded."""
    from music_assistant_client import MusicAssistantClient

    ma = MusicAssistantClient(run.ma_url, None, token=run.token)
    ma.subscribe(events.record)
    ready = asyncio.Event()
    listener = asyncio.create_task(ma.start_listening(ready))
    waiter = asyncio.create_task(ready.wait())
    async with asyncio.timeout(CONNECT_TIMEOUT_S):
        await asyncio.wait([listener, waiter], return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if listener.done():
        # a failed connect or auth ends the listener; surface its error instead of a timeout
        listener.result()
    try:
        yield ma
    finally:
        await ma.disconnect()
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


def _parametrize_device(metafunc: pytest.Metafunc) -> None:
    """Parametrize `device` over the leased devices of the marker's providers, else all leased."""
    leased = from_env(os.environ["LAB_DEVICES"])
    marker = metafunc.definition.get_closest_marker("devices")
    providers = marker.args if marker is not None else None
    devices = sorted(
        device_id
        for device_id, device in leased.items()
        if providers is None or device.provider in providers
    )
    if devices:
        metafunc.parametrize("device", devices)
        return
    # nothing leased still shows up in the report, as skipped
    wanted = " or ".join(providers) if providers else "device"
    skip = pytest.mark.skip(reason=f"no {wanted} leased")
    metafunc.parametrize("device", [pytest.param(None, marks=skip)])


def _roles() -> tuple[str, ...]:
    """Player roles of the run from LAB_ROLES."""
    return tuple(role for role in os.environ["LAB_ROLES"].split(",") if role)
