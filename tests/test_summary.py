"""Readable run outcome from summary.json and JUnit."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from labctl.summary import format_run, format_runs

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="scenarios.gate.test_queue" name="test_queue[slim-1]">
<properties><property name="area" value="queue"/><property name="checkpoint" value="play"/>
<property name="checkpoint" value="next"/><property name="issues" value="6860"/></properties>
<failure/></testcase>
<testcase classname="scenarios.gate.test_queue" name="test_queue[dlna-1]">
<properties><property name="area" value="queue"/><property name="checkpoint" value="play"/>
<property name="failed_checkpoint" value="seek"/><property name="failed_issues" value="6752"/>
</properties><error/></testcase>
<testcase classname="scenarios.gate.test_queue" name="test_queue[snap-1]">
<properties><property name="area" value="queue"/></properties></testcase>
<testcase classname="scenarios.smoke.test_smoke" name="test_smoke[slim-1]"><skipped/></testcase>
</testsuite></testsuites>
"""


def summary(exit_code: int | None, **extra: Any) -> dict[str, Any]:
    """Return a summary.json dict of run r1."""
    return {
        "run_id": "r1",
        "ref": "pr/7244",
        "sha": "0123456789abcdef",
        "duration_s": 61.4,
        "exit_code": exit_code,
        **extra,
    }


def junit(tmp_path: Path) -> Path:
    """Write the synthetic JUnit file and return its path."""
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    return path


def test_green(tmp_path: Path) -> None:
    """A passing run is one line."""
    assert format_run(summary(0), junit(tmp_path)) == "run r1: green (4 tests, 61 s)"


def test_failed_before_pytest(tmp_path: Path) -> None:
    """No exit code means the run never reached pytest."""
    assert format_run(summary(None), tmp_path / "junit.xml") == (
        "run r1: pr/7244 at 0123456789ab, failed before pytest, 61 s"
    )


def test_red_with_checkpoints_and_rerun(tmp_path: Path) -> None:
    """Failures list under their area with the failed or last checkpoint, then the rerun."""
    slim = "/src/scenarios/gate/test_queue.py::test_queue[slim-1]"
    dlna = "/src/scenarios/gate/test_queue.py::test_queue[dlna-1]"
    reruns = [
        {"test": slim, "first": "fail", "second": "pass"},
        {"test": dlna, "first": "fail", "second": "fail"},
    ]
    text = format_run(summary(1, rerun_run_id="r2", reruns=reruns), junit(tmp_path))
    assert text.splitlines() == [
        "run r1: pr/7244 at 0123456789ab, exit 1, 61 s",
        "area queue: 1/3 passed",
        f"  FAIL {slim}  checkpoint next  issues 6860",
        f"  FAIL {dlna}  checkpoint seek  issues 6752",
        "area -: 0/0 passed, 1 skipped",
        "rerun r2: 1 of 2 passed on rerun",
        f"  {slim}: first fail, second pass",
        f"  {dlna}: first fail, second fail",
    ]


def test_format_runs(tmp_path: Path) -> None:
    """Newest first within range and ref; an unfinished run and a boot failure read apart."""
    runs: dict[str, tuple[str, dict[str, Any] | None]] = {
        "20261001-080000-aaaa": ("dev", summary(0)),
        "20261028-080000-bbbb": ("dev", summary(1, rerun_run_id="20261028-081000-cccc")),
        "20261028-090000-dddd": ("dev", summary(None)),
        "20261028-100000-eeee": ("dev", None),
        "20261028-110000-ffff": ("stable", summary(0)),
    }
    for run_id, (ref, data) in runs.items():
        (tmp_path / run_id).mkdir()
        meta = {"ref": ref, "sha": "0123456789abcdef", "purpose": f"gate {ref} queue"}
        (tmp_path / run_id / "run.json").write_text(json.dumps(meta))
        if data is not None:
            (tmp_path / run_id / "summary.json").write_text(json.dumps(data))
    now = datetime(2026, 10, 29, tzinfo=UTC)
    head = "0123456789ab  gate dev queue"
    assert format_runs(tmp_path, "dev", 7, now).splitlines() == [
        f"20261028-100000-eeee  dev  {head}  exit -  -",
        f"20261028-090000-dddd  dev  {head}  exit boot-failed  61 s",
        f"20261028-080000-bbbb  dev  {head}  exit 1  61 s  rerun 20261028-081000-cccc",
    ]
    assert format_runs(tmp_path, "pr/1", 7, now) == "no runs in the last 7 days"
