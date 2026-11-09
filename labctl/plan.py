"""What one `lab run` starts: the runs its flags and --providers select, in order."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from labctl import areas
from labctl.runs import parse_roles, role_kind


@dataclass(frozen=True)
class RunSpec:
    """One run of a `lab run`: its roles, pytest marker, leased resources and whether it reruns."""

    mode: str
    purpose: str
    roles: tuple[str, ...]
    marker: str
    # gate areas recorded in run.json and summary.json
    areas: tuple[str, ...] = ()
    account_set: str | None = None
    devices: tuple[str, ...] = ()
    rerun: bool = False
    deep: bool = False


def plan(
    ref: str,
    selected: areas.Providers,
    gate_areas: tuple[str, ...],
    all_areas: bool,
    deep: bool,
    keep: bool,
    test_expr: str | None,
    frozen_sets: Iterable[str],
) -> tuple[RunSpec, ...]:
    """Return the runs to start, in order; raise ValueError for flags that contradict each other.

    `frozen_sets` are the account sets a deep run covers after the virtual players.
    """
    _check(selected, bool(gate_areas), all_areas, deep, keep)
    listed = parse_roles(",".join(selected.kinds)) if selected.kinds else ()
    if deep:
        return _deep(ref, listed or areas.DEEP_ROLES, test_expr, frozen_sets)
    specs: list[RunSpec] = []
    if all_areas:
        everything = tuple(areas.AREA_ROLES)
        purpose = areas.purpose("all", ref, keep=keep)
        specs.append(RunSpec("all", purpose, areas.all_roles(), "gate", everything, rerun=True))
    elif gate_areas:
        domains = ",".join(_domains(listed))
        purpose = areas.purpose("gate", ref, ",".join(gate_areas), domains, keep=keep)
        roles = _with_core(listed) if listed else areas.gate_roles(gate_areas)
        marker = areas.gate_expression(gate_areas)
        specs.append(RunSpec("gate", purpose, roles, marker, gate_areas, rerun=True))
    elif selected.devices:
        purpose = areas.purpose("bench", ref, ",".join(selected.devices), keep=keep)
        # without virtual roles only the bench scenarios have a player to run on
        marker = "smoke or bench" if listed else "bench"
        specs.append(RunSpec("bench", purpose, listed, marker, devices=selected.devices))
    elif selected.account_sets:
        # the listed players are the stage of the music runs below, not a gate of their own
        pass
    elif listed:
        purpose = areas.purpose("gate", ref, ",".join(_domains(listed)), keep=keep)
        marker = areas.gate_expression((), _domains(listed))
        roles = _with_core(areas.with_journey_roles(listed))
        specs.append(RunSpec("gate", purpose, roles, marker, rerun=True))
    else:
        # the smoke test plays on every role, so only the kinds with audio
        roles = parse_roles(",".join(areas.PLAYER_DOMAINS))
        purpose = areas.purpose("smoke", ref, keep=keep)
        specs.append(RunSpec("smoke", purpose, roles, "smoke"))
    stage = listed[:1] or ("sendspin-1",)
    for account_set in selected.account_sets:
        purpose = areas.purpose("music", ref, account_set, keep=keep)
        specs.append(RunSpec("music", purpose, stage, "music", account_set=account_set))
    return tuple(specs)


def _check(selected: areas.Providers, gate: bool, all_areas: bool, deep: bool, keep: bool) -> None:
    """Raise ValueError for flag combinations `lab run` refuses."""
    if sum((gate, all_areas, deep)) > 1:
        raise ValueError("--areas, --all and --deep exclude each other")
    if deep and keep:
        raise ValueError("--deep starts several runs, --keep keeps one")
    if deep and (selected.account_sets or selected.devices):
        raise ValueError("--deep takes player providers only")
    if all_areas and selected != areas.Providers():
        raise ValueError("--all runs every provider, drop --providers")
    if keep and len(selected.account_sets) > 1:
        raise ValueError("--keep keeps one run, name one music provider")
    if gate and selected.devices:
        raise ValueError("gates do not collect bench scenarios, run the devices without --areas")
    if selected.devices and any(kind != "sendspin" for kind in selected.kinds):
        # the other kinds are discovered over the network device runs share, so two runs
        # would see each other's players under the same names
        raise ValueError("devices combine with sendspin players only")


def _deep(
    ref: str, roles: tuple[str, ...], test_expr: str | None, frozen_sets: Iterable[str]
) -> tuple[RunSpec, ...]:
    """Return the deep run, then a music run per frozen account set on `sendspin-1`."""
    specs = [RunSpec("deep", areas.purpose("deep", ref), roles, "deep", deep=True)]
    # a -k expression names specific deep tests, so it skips the account sets
    if not test_expr:
        specs += [
            RunSpec(
                "deep",
                areas.purpose("deep", ref, name),
                ("sendspin-1",),
                "music",
                account_set=name,
                deep=True,
            )
            for name in sorted(frozen_sets)
        ]
    return tuple(specs)


def _with_core(roles: tuple[str, ...]) -> tuple[str, ...]:
    """Return core's own role ahead of `roles`, so core never skips for want of its player."""
    return tuple(dict.fromkeys((*areas.AREA_ROLES[areas.CORE_AREA], *roles)))


def _domains(roles: tuple[str, ...]) -> tuple[str, ...]:
    """Return the provider domains of `roles`, each once."""
    kinds = dict.fromkeys(role_kind(role) for role in roles)
    return tuple(areas.PLAYER_DOMAINS[kind] for kind in kinds)
