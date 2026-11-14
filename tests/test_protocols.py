"""Finding players and protocols in the player state of both player models."""

from typing import Any

import pytest

from labctl.protocols import player_of_protocol, protocol_id, protocol_ids, protocol_key

# before the player split: the MPD player is the protocol, AirPlay sits behind a universal player
UNIVERSAL = {
    "player_id": "up8f3a",
    "protocols": [],
    "output_protocols": [
        {"output_protocol_id": "ap1a2b3c4d5e6f", "protocol_domain": "airplay"},
    ],
}
MPD = {"player_id": "mpd_mpd-1_6600", "output_protocols": []}
# with the split: one player per device that lists its protocols
SPLIT_AIRPLAY = {
    "player_id": "pl3c0ffee12345",
    "protocols": [{"protocol_id": "ap1a2b3c4d5e6f", "domain": "airplay"}],
    "output_protocols": [{"output_protocol_id": "native", "protocol_domain": "airplay"}],
}
SPLIT_MPD = {
    "player_id": "pld900f2f1aeeb",
    "protocols": [{"protocol_id": "mpd_mpd-1_6600", "domain": "mpd"}],
    "output_protocols": [],
}


@pytest.mark.parametrize(
    ("player", "expected"),
    [(MPD, ["mpd_mpd-1_6600"]), (UNIVERSAL, ["up8f3a"]), (SPLIT_MPD, ["mpd_mpd-1_6600"])],
)
def test_protocol_ids(player: dict[str, Any], expected: list[str]) -> None:
    """A player stands for its listed protocols, else for the protocol under its own id."""
    assert protocol_ids(player) == expected


@pytest.mark.parametrize("players", [[UNIVERSAL, MPD], [SPLIT_AIRPLAY, SPLIT_MPD]])
def test_player_of_protocol(players: list[dict[str, Any]]) -> None:
    """The MPD protocol resolves to its player on both models; an unknown id to none."""
    assert player_of_protocol(players, "mpd_mpd-1_6600") is players[1]
    assert player_of_protocol(players, "mpd_mpd-2_6600") is None


@pytest.mark.parametrize("player", [UNIVERSAL, SPLIT_AIRPLAY])
def test_protocol_id(player: dict[str, Any]) -> None:
    """The AirPlay protocol is found behind the universal player and in the split player."""
    assert protocol_id(player, "airplay") == "ap1a2b3c4d5e6f"


def test_protocol_key() -> None:
    """A protocol setting is keyed by the protocol id in its player's config."""
    assert protocol_key("ap1a2b3c4d5e6f", "streaming_mode") == (
        "ap1a2b3c4d5e6f||protocol||streaming_mode"
    )
