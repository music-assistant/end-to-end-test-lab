"""Flake ledger: recording gate reruns and reporting them per check."""

import json
from datetime import UTC, datetime
from pathlib import Path

from labctl import flakes

QUEUE = "/src/scenarios/gate/test_queue.py::test_queue"
SEEK = "/src/scenarios/gate/test_seek.py::test_seek"


def test_record_then_report(tmp_path: Path) -> None:
    """Each rerun is one line; the report counts per check, most rerun first."""
    path = tmp_path / "flakes.jsonl"
    flakes.record(
        path,
        "r1",
        "dev",
        [
            {"test": f"{SEEK}[slim-1]", "first": "fail", "second": "fail"},
            {"test": f"{QUEUE}[slim-1]", "first": "fail", "second": "pass"},
        ],
        {f"{SEEK}[slim-1]": "seek", f"{QUEUE}[slim-1]": "play"},
        {f"{SEEK}[slim-1]": "resume"},
    )
    flakes.record(
        path,
        "r3",
        "pr/7244",
        [
            {"test": f"{QUEUE}[dlna-1]", "first": "fail", "second": "fail"},
        ],
        {},
        {},
    )
    first = json.loads(path.read_text().splitlines()[0])
    assert {k: v for k, v in first.items() if k != "date"} == {
        "run_id": "r1",
        "ref": "dev",
        "test": f"{SEEK}[slim-1]",
        "role": "slim-1",
        "check": "gate/test_seek.py::test_seek",
        "first": "fail",
        "second": "fail",
        "first_checkpoint": "seek",
        "second_checkpoint": "resume",
    }
    assert flakes.report(path).splitlines() == [
        "gate/test_queue.py::test_queue  reruns 2  passed on rerun 1  roles dlna-1,slim-1",
        "gate/test_seek.py::test_seek  reruns 1  passed on rerun 0  roles slim-1",
    ]


def test_report_range(tmp_path: Path) -> None:
    """Entries older than `days` drop out."""
    path = tmp_path / "flakes.jsonl"
    lines = [
        {
            "date": "2026-10-01T00:00:00+00:00",
            "test": QUEUE,
            "check": "q",
            "role": "-",
            "second": "pass",
        },
        {
            "date": "2026-10-20T00:00:00+00:00",
            "test": SEEK,
            "check": "s",
            "role": "-",
            "second": "fail",
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    now = datetime(2026, 10, 27, tzinfo=UTC)
    assert flakes.report(path, 14, now) == "s  reruns 1  passed on rerun 0  roles -"
    assert flakes.report(path, 3, now) == "no reruns in the last 3 days"


def test_missing_file(tmp_path: Path) -> None:
    """No ledger yet means no reruns."""
    assert flakes.report(tmp_path / "flakes.jsonl") == "no reruns in the last 30 days"


def test_listing(tmp_path: Path) -> None:
    """Every entry in range, newest first; a line written before checkpoints shows `-`."""
    path = tmp_path / "flakes.jsonl"
    base = {"run_id": "r1", "ref": "dev", "first": "fail"}
    lines = [
        {**base, "date": "2026-10-01T00:00:00+00:00", "test": SEEK, "second": "pass"},
        {**base, "date": "2026-10-20T00:00:00+00:00", "test": SEEK, "second": "fail"},
        {
            **base,
            "date": "2026-10-21T00:00:00+00:00",
            "test": QUEUE,
            "second": "pass",
            "first_checkpoint": "next",
            "second_checkpoint": "-",
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    now = datetime(2026, 10, 27, tzinfo=UTC)
    assert flakes.listing(path, 14, now).splitlines() == [
        f"2026-10-21T00:00:00+00:00  r1  dev  {QUEUE}  fail (next) -> pass (-)",
        f"2026-10-20T00:00:00+00:00  r1  dev  {SEEK}  fail (-) -> fail (-)",
    ]
