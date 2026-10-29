"""Gate areas, and the providers `--providers` names: player kinds, account sets, bench devices."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# the gate always runs this area, whatever --areas names
CORE_AREA = "core"
# gate roles per area; a run started with --areas boots the union of the selected areas' roles
AREA_ROLES: dict[str, tuple[str, ...]] = {
    CORE_AREA: ("slim-1",),
    "queue": ("airplay-1", "dlna-1", "slim-1", "snap-1", "sendspin-1"),
    "streams": ("slim-1", "sendspin-1"),
    "players": ("slim-1", "slim-2"),
    "library": ("slim-1",),
}
# MA provider domain per virtual player kind; --providers takes the domain or the kind
PLAYER_DOMAINS = {
    "sendspin": "sendspin",
    "airplay": "airplay",
    "dlna": "dlna",
    "slim": "squeezelite",
    "snap": "snapcast",
    "mpd": "mpd",
}
# provider domain per account set name prefix: set `apple-a` holds an `apple_music` provider
ACCOUNT_SET_PROVIDERS = {
    "spotify": "spotify",
    "apple": "apple_music",
    "tidal": "tidal",
    "ytm": "ytmusic",
    "qobuz": "qobuz",
    "deezer": "deezer",
}
# tier 2 areas, one per frozen account provider; selected by --providers, never by --areas
MUSIC_AREAS = tuple(ACCOUNT_SET_PROVIDERS.values())
# every role a deep run boots: the deep matrices want two of slim, snap and sendspin
DEEP_ROLES = (
    "sendspin-1",
    "sendspin-2",
    "airplay-1",
    "dlna-1",
    "slim-1",
    "slim-2",
    "snap-1",
    "snap-2",
    "mpd-1",
)
# --providers word for every registered bench device
ALL_DEVICES = "devices"


@dataclass(frozen=True)
class Providers:
    """What a --providers value selects."""

    # virtual player kinds in the given order, repeated for more players of a kind
    kinds: tuple[str, ...] = ()
    account_sets: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()


def parse_providers(value: str, account_sets: Iterable[str], devices: Iterable[str]) -> Providers:
    """Resolve player domains or kinds, music domains or set names, and device ids or `devices`.

    Raises ValueError for a name that is none of these, or a music domain without exactly one set.
    """
    sets_known, devices_known = sorted(account_sets), sorted(devices)
    # the lab's own kind names are accepted too, `slim` for `squeezelite`
    kind_by_name = {**{d: k for k, d in PLAYER_DOMAINS.items()}, **{k: k for k in PLAYER_DOMAINS}}
    kinds: list[str] = []
    sets: list[str] = []
    selected_devices: set[str] = set()
    for name in (part.strip() for part in value.split(",")):
        if name in kind_by_name:
            kinds.append(kind_by_name[name])
        elif name == ALL_DEVICES:
            if not devices_known:
                raise ValueError("the [devices] registry is empty")
            selected_devices.update(devices_known)
        elif name in devices_known:
            selected_devices.add(name)
        elif name in sets_known:
            sets.append(name)
        elif name in MUSIC_AREAS:
            sets.append(_account_set(name, sets_known))
        else:
            known = [*PLAYER_DOMAINS.values(), *MUSIC_AREAS, *devices_known, ALL_DEVICES]
            msg = f"unknown provider {name!r}, known: {', '.join(known)}"
            raise ValueError(msg)
    return Providers(tuple(kinds), tuple(dict.fromkeys(sets)), tuple(sorted(selected_devices)))


def account_set_provider(account_set: str) -> str:
    """Return the music provider domain of an account set, `apple_music` for `apple-a`."""
    return ACCOUNT_SET_PROVIDERS[account_set.rsplit("-", 1)[0]]


def account_set_area(account_set: str) -> str:
    """Return the tier 2 area of an account set, `music_apple_music` for `apple-a`."""
    return f"music_{account_set_provider(account_set)}"


def purpose(mode: str, ref: str, *parts: str, keep: bool = False) -> str:
    """Return what a run is for, as `lab status` and `lab runs` show it: `gate pr/7244 queue`."""
    words = [mode, ref, *(part for part in parts if part)]
    return " ".join([*words, "keep"] if keep else words)


def area_marker(area: str) -> str:
    """Return the pytest marker of an area, `area_queue` for `queue`."""
    return f"area_{area}"


def provider_marker(domain: str) -> str:
    """Return the pytest marker of a player provider's journey, `provider_airplay`."""
    return f"provider_{domain}"


def gate_expression(areas: tuple[str, ...], domains: tuple[str, ...] = ()) -> str:
    """Return the pytest -m expression of a gate over `areas` and the journeys of `domains`.

    Core is always included.
    """
    selected = (CORE_AREA, *(area for area in areas if area != CORE_AREA))
    markers = [*map(area_marker, selected), *map(provider_marker, domains)]
    return f"gate and ({' or '.join(markers)})"


def gate_roles(areas: tuple[str, ...]) -> tuple[str, ...]:
    """Return the roles a gate run over `areas` boots, in a stable order."""
    roles: list[str] = []
    for area in (CORE_AREA, *areas):
        roles += [role for role in AREA_ROLES[area] if role not in roles]
    return tuple(roles)


def all_roles() -> tuple[str, ...]:
    """Return the roles of a run over every area and journey: the areas' plus one of each kind."""
    roles = gate_roles(tuple(AREA_ROLES))
    return roles + tuple(f"{k}-1" for k in PLAYER_DOMAINS if f"{k}-1" not in roles)


def _account_set(domain: str, account_sets: list[str]) -> str:
    """Return the one frozen set holding a music provider domain."""
    matching = [s for s in account_sets if ACCOUNT_SET_PROVIDERS.get(s.rsplit("-", 1)[0]) == domain]
    if len(matching) != 1:
        found = ", ".join(matching) if matching else "none"
        msg = f"{domain} needs exactly one frozen account set, found {found}; name the set"
        raise ValueError(msg)
    return matching[0]
