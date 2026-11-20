"""Known reds: checkpoints listed in `scenarios/known_red.toml` that fail for a known reason."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

FILE = Path(__file__).parents[1] / "known_red.toml"


@dataclass(frozen=True)
class KnownRed:
    """A listed checkpoint, optionally limited to one role or device and one account set."""

    step: str
    since: str
    note: str
    role: str | None = None
    account_set: str | None = None


def load(path: Path) -> list[KnownRed]:
    """Return the entries of a known red file."""
    with path.open("rb") as fh:
        return [KnownRed(**entry) for entry in tomllib.load(fh).get("known_red", [])]


def applying(
    entries: Iterable[KnownRed], subject: str, account_set: str | None
) -> dict[str, KnownRed]:
    """Return the entries that apply to a test on `subject` under `account_set`, by step."""
    return {
        entry.step: entry
        for entry in entries
        if entry.role in (None, subject) and entry.account_set in (None, account_set)
    }


def xfail_reason(user_properties: Iterable[tuple[str, object]]) -> str:
    """Return the xfail reason of a test from its recorded known reds, `""` without one."""
    props = list(user_properties)
    steps = [value for name, value in props if name == "known_red"]
    notes = [value for name, value in props if name == "known_red_note"]
    return "; ".join(f"known red {step}: {note}" for step, note in zip(steps, notes, strict=True))
