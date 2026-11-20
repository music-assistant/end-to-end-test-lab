"""Flake ledger: every gate rerun outcome, appended to one JSONL file, and a report per check."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

SRC_PREFIX = "/src/scenarios/"


def record(
    path: Path,
    run_id: str,
    ref: str,
    reruns: list[dict[str, str]],
    first_checkpoints: dict[str, str],
    second_checkpoints: dict[str, str],
) -> None:
    """Append one ledger line per rerun entry of gate run `run_id`, with each run's checkpoint.

    The checkpoints map the failed node ids of the first run and of the rerun to theirs.
    """
    date = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a") as fh:
        for entry in reruns:
            check, role = _split(entry["test"])
            line = {
                "date": date,
                "run_id": run_id,
                "ref": ref,
                "test": entry["test"],
                "role": role,
                "check": check,
                "first": entry["first"],
                "second": entry["second"],
                "first_checkpoint": first_checkpoints.get(entry["test"], "-"),
                "second_checkpoint": second_checkpoints.get(entry["test"], "-"),
            }
            fh.write(json.dumps(line) + "\n")


def report(path: Path, days: int = 30, now: datetime | None = None) -> str:
    """Return reruns per check over the last `days` days, flaky and steady red apart."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    entries = [e for e in _entries(path) if datetime.fromisoformat(e["date"]) >= cutoff]
    if not entries:
        return f"no reruns in the last {days} days"
    reruns = Counter(e["check"] for e in entries)
    flaky = [e for e in entries if e["second"] == "pass"]
    steady = [e for e in entries if e["second"] != "pass"]
    return "\n".join(
        _section("flaky (failed, then passed on rerun)", flaky, reruns)
        + _section("steady red (failed twice)", steady, reruns)
    )


def listing(path: Path, days: int = 30, now: datetime | None = None) -> str:
    """Return the ledger's entries of the last `days` days, newest first, one per line."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    entries = [e for e in _entries(path) if datetime.fromisoformat(e["date"]) >= cutoff]
    if not entries:
        return f"no reruns in the last {days} days"
    # ledger lines written before checkpoints were recorded lack them
    return "\n".join(
        f"{e['date']}  {e['run_id']}  {e['ref']}  {e['test']}  "
        f"{e['first']} ({e.get('first_checkpoint', '-')}) -> "
        f"{e['second']} ({e.get('second_checkpoint', '-')})"
        for e in sorted(entries, key=lambda e: e["date"], reverse=True)
    )


def _section(title: str, entries: list[dict[str, str]], reruns: Counter[str]) -> list[str]:
    """Title and one line per check of a report section, most entries first; none when empty."""
    if not entries:
        return []
    counts = Counter(e["check"] for e in entries)
    roles: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        roles[entry["check"]].add(entry["role"])
    checks = sorted(counts, key=lambda c: (-counts[c], c))
    return [title] + [
        f"{c}  {counts[c]} of {reruns[c]} reruns  roles {','.join(sorted(roles[c]))}"
        for c in checks
    ]


def _split(test: str) -> tuple[str, str]:
    """Return the check and role of a node id; the role is its parametrization id or `-`."""
    check = test.removeprefix(SRC_PREFIX)
    if check.endswith("]") and "[" in check:
        check, role = check[:-1].split("[", 1)
        return check, role
    return check, "-"


def _entries(path: Path) -> list[dict[str, str]]:
    """Return the ledger's entries; a missing file has none."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]
