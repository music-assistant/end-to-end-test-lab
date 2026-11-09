"""Gate areas, provider parsing and run purposes."""

import pytest

from labctl import areas

SETS = ("spotify-a", "apple-a", "tidal-a", "tidal-b")
DEVICES = ("sonos-1", "wiim-1")


@pytest.mark.parametrize(
    ("account_set", "area"),
    [("spotify-a", "music_spotify"), ("apple-a", "music_apple_music"), ("ytm-b", "music_ytmusic")],
)
def test_account_set_area(account_set: str, area: str) -> None:
    """The set name prefix maps to the provider's music area."""
    assert areas.account_set_area(account_set) == area


def test_gate_expression() -> None:
    """Core always runs; provider journeys join the areas."""
    assert areas.gate_expression(("queue",)) == "gate and (area_core or area_queue)"
    assert areas.gate_expression((), ("airplay", "squeezelite")) == (
        "gate and (area_core or provider_airplay or provider_squeezelite)"
    )


def test_all_roles() -> None:
    """A run over everything boots every area's roles, one of each kind and the journeys' roles."""
    assert areas.all_roles() == (
        "slim-1",
        "airplay-1",
        "dlna-1",
        "snap-1",
        "sendspin-1",
        "slim-2",
        "mpd-1",
        "sendspin-2",
        "sendspinviz-1",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("squeezelite,slim,snapcast", areas.Providers(kinds=("slim", "slim", "snap"))),
        ("spotify,apple-a,spotify", areas.Providers(account_sets=("spotify-a", "apple-a"))),
        ("tidal-b,airplay", areas.Providers(kinds=("airplay",), account_sets=("tidal-b",))),
        ("wiim-1,sonos-1,sendspin", areas.Providers(kinds=("sendspin",), devices=DEVICES)),
        ("devices", areas.Providers(devices=DEVICES)),
    ],
)
def test_parse_providers(value: str, expected: areas.Providers) -> None:
    """Domains and kind names give players, music domains and set names sets, ids devices."""
    assert areas.parse_providers(value, SETS, DEVICES) == expected


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("chromecast", "unknown provider 'chromecast'"),
        ("qobuz", "qobuz needs exactly one frozen account set, found none"),
        ("tidal", "found tidal-a, tidal-b"),
    ],
)
def test_parse_providers_errors(value: str, error: str) -> None:
    """Unknown names fail, and a music domain must resolve to exactly one set."""
    with pytest.raises(ValueError, match=error):
        areas.parse_providers(value, SETS, DEVICES)


def test_purpose() -> None:
    """Empty parts drop out and a kept run says so."""
    assert areas.purpose("gate", "pr/7244", "queue", "") == "gate pr/7244 queue"
    assert areas.purpose("all", "dev", keep=True) == "all dev keep"
