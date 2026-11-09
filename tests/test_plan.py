"""Which runs a `lab run` starts for its flags and providers."""

import pytest

from labctl.areas import Providers
from labctl.plan import RunSpec, plan

SETS = ("spotify-a", "tidal-a")
ALL_KINDS = ("sendspin-1", "airplay-1", "dlna-1", "slim-1", "snap-1", "mpd-1")
NOTHING = Providers()


def specs(
    selected: Providers = NOTHING,
    gate_areas: tuple[str, ...] = (),
    all_areas: bool = False,
    deep: bool = False,
    keep: bool = False,
    test_expr: str | None = None,
) -> tuple[RunSpec, ...]:
    """Plan a run of `dev` against the two frozen sets."""
    return plan("dev", selected, gate_areas, all_areas, deep, keep, test_expr, SETS)


def test_areas() -> None:
    """A gate over the areas on their roles, rerun once."""
    assert specs(gate_areas=("queue",)) == (
        RunSpec(
            "gate",
            "gate dev queue",
            ("slim-1", "airplay-1", "dlna-1", "snap-1", "sendspin-1"),
            "gate and (area_core or area_queue)",
            ("queue",),
            rerun=True,
        ),
    )


def test_areas_with_players() -> None:
    """Listed players narrow the gate, and core keeps its own role."""
    [spec] = specs(Providers(kinds=("airplay",)), gate_areas=("queue",))
    assert (spec.purpose, spec.roles) == ("gate dev queue airplay", ("slim-1", "airplay-1"))


def test_players_alone() -> None:
    """Player providers alone run core plus their journeys, also with -k."""
    assert specs(Providers(kinds=("airplay",)), test_expr="next") == (
        RunSpec(
            "gate",
            "gate dev airplay",
            ("slim-1", "airplay-1"),
            "gate and (area_core or provider_airplay)",
            rerun=True,
        ),
    )


def test_players_alone_journey_roles() -> None:
    """A journey that needs company boots it: Sendspin a second player and a visualizer."""
    [spec] = specs(Providers(kinds=("sendspin",)))
    assert spec.roles == ("slim-1", "sendspin-1", "sendspin-2", "sendspinviz-1")


def test_all() -> None:
    """Everything marked gate, on every area's roles plus one of each kind."""
    [spec] = specs(all_areas=True, keep=True)
    assert (spec.purpose, spec.marker, spec.rerun) == ("all dev keep", "gate", True)
    assert spec.areas == ("core", "queue", "streams", "players", "library")
    assert set(spec.roles) >= set(ALL_KINDS)


def test_music_alone() -> None:
    """One music run per set on sendspin-1, no rerun."""
    assert specs(Providers(account_sets=SETS)) == (
        RunSpec("music", "music dev spotify-a", ("sendspin-1",), "music", account_set="spotify-a"),
        RunSpec("music", "music dev tidal-a", ("sendspin-1",), "music", account_set="tidal-a"),
    )


def test_music_on_player_stage() -> None:
    """A listed player is the music run's stage, not a gate of its own."""
    [spec] = specs(Providers(kinds=("airplay",), account_sets=("spotify-a",)))
    assert (spec.mode, spec.roles) == ("music", ("airplay-1",))


def test_devices() -> None:
    """Devices alone run only the bench scenarios."""
    assert specs(Providers(devices=("sonos-1",))) == (
        RunSpec("bench", "bench dev sonos-1", (), "bench", devices=("sonos-1",)),
    )


def test_devices_with_sendspin() -> None:
    """Sendspin players join the device run, which then runs the smoke test too."""
    [spec] = specs(Providers(kinds=("sendspin",), devices=("sonos-1",)))
    assert (spec.roles, spec.marker) == (("sendspin-1",), "smoke or bench")


def test_deep() -> None:
    """The deep run on every deep role, then every frozen set on sendspin-1."""
    deep, spotify, tidal = specs(deep=True)
    assert (deep.purpose, deep.marker, deep.deep, len(deep.roles)) == ("deep dev", "deep", True, 10)
    assert spotify == RunSpec(
        "deep", "deep dev spotify-a", ("sendspin-1",), "music", account_set="spotify-a", deep=True
    )
    assert tidal.account_set == "tidal-a"


def test_deep_with_players() -> None:
    """Listed players replace the deep roles; the sets still follow."""
    deep, *sets = specs(Providers(kinds=("slim", "slim")), deep=True)
    assert (deep.roles, len(sets)) == (("slim-1", "slim-2"), 2)


def test_deep_with_k_skips_sets() -> None:
    """A -k expression names deep tests, so no set runs."""
    assert [spec.marker for spec in specs(deep=True, test_expr="rejoin")] == ["deep"]


def test_nothing_listed() -> None:
    """The smoke run on one player of each kind."""
    assert specs() == (RunSpec("smoke", "smoke dev", ALL_KINDS, "smoke"),)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"gate_areas": ("queue",), "deep": True}, "exclude each other"),
        ({"all_areas": True, "deep": True}, "exclude each other"),
        ({"deep": True, "keep": True}, "--keep keeps one"),
        ({"selected": Providers(account_sets=("spotify-a",)), "deep": True}, "player providers"),
        ({"selected": Providers(kinds=("airplay",)), "all_areas": True}, "drop --providers"),
        ({"selected": Providers(account_sets=SETS), "keep": True}, "one music provider"),
        ({"selected": Providers(devices=("sonos-1",)), "gate_areas": ("queue",)}, "bench"),
        ({"selected": Providers(kinds=("airplay",), devices=("sonos-1",))}, "sendspin players"),
    ],
)
def test_refused(kwargs: dict[str, object], error: str) -> None:
    """Contradicting flags are refused before anything is leased."""
    with pytest.raises(ValueError, match=error):
        specs(**kwargs)  # type: ignore[arg-type]
