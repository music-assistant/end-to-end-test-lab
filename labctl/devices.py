"""Real devices of the bench: registry entries and how a server player resolves to one."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass

# real speakers on the bench have no capture and sit in a room, so keep them quiet
DEVICE_VOLUME = 1


@dataclass(frozen=True)
class Device:
    """A registry entry: the MA provider domain that serves the device and its player name."""

    provider: str
    name: str


def matches(device: Device, name: str, provider: str, protocol_domains: Iterable[str]) -> bool:
    """Return whether a player (name, provider instance id, output protocol domains) is `device`."""
    # names alone are ambiguous: a Sonos and a HomePod in one room both advertise the room name
    return name == device.name and (
        provider == device.provider
        or provider.startswith(f"{device.provider}--")
        or device.provider in protocol_domains
    )


def to_env(devices: dict[str, Device]) -> str:
    """Serialize devices by id for the LAB_DEVICES environment variable."""
    return json.dumps(
        {device_id: asdict(device) for device_id, device in devices.items()}, sort_keys=True
    )


def from_env(text: str) -> dict[str, Device]:
    """Parse a LAB_DEVICES value back into devices by id."""
    return {device_id: Device(**entry) for device_id, entry in json.loads(text).items()}
