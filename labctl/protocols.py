"""Protocols of the players a server publishes, on both of its player models.

Servers before the player split publish each protocol of a device as a player under the
protocol's id, with a universal player in front of the protocol players of a device that has no
native player. Servers with the split publish one player per device, with an id of its own, and
list the protocols it is made up of in `protocols`; servers before it leave that list empty or
leave it out.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# a player's config holds the settings of its protocols as `<protocol id>||protocol||<key>`
PROTOCOL_KEY_SPLITTER = "||protocol||"


def protocol_ids(player: dict[str, Any]) -> list[str]:
    """Return the ids of the protocols a published player stands for."""
    if protocols := player.get("protocols"):
        return [protocol["protocol_id"] for protocol in protocols]
    return [player["player_id"]]


def player_of_protocol(
    players: Iterable[dict[str, Any]], protocol_id: str
) -> dict[str, Any] | None:
    """Return the published player a protocol belongs to, None while there is none."""
    return next((player for player in players if protocol_id in protocol_ids(player)), None)


def protocol_id(player: dict[str, Any], domain: str) -> str:
    """Return the id of the player's protocol of a provider domain, behind it or part of it."""
    if protocols := player.get("protocols"):
        [found] = [p["protocol_id"] for p in protocols if p["domain"] == domain]
    else:
        [found] = [
            output["output_protocol_id"]
            for output in player["output_protocols"]
            if output["protocol_domain"] == domain
        ]
    return str(found)


def protocol_key(protocol_id: str, key: str) -> str:
    """Return the key of a protocol's setting in the config of the player it belongs to."""
    return f"{protocol_id}{PROTOCOL_KEY_SPLITTER}{key}"
