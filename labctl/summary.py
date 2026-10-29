"""Readable outcome of a finished run: failures per area with their checkpoint, and reruns."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from labctl import runs

# area of a testcase that records none
NO_AREA = "-"


def format_run(summary: dict[str, Any], junit: Path) -> str:
    """Return the printed outcome of a run from its summary.json and JUnit file."""
    head = f"run {summary['run_id']}"
    duration = f"{summary['duration_s']:.0f} s"
    at = f"{summary['ref']} at {summary['sha'][:12]}"
    if summary["exit_code"] is None:
        return f"{head}: {at}, failed before pytest, {duration}"
    # pytest writes no JUnit file when it exits on a usage error
    cases = runs.testcases(junit) if junit.exists() else {}
    if summary["exit_code"] == 0:
        return f"{head}: green ({len(cases)} tests, {duration})"
    lines = [f"{head}: {at}, exit {summary['exit_code']}, {duration}"]
    for area, area_cases in _by_area(cases).items():
        lines += _area_lines(area, area_cases)
    if "reruns" in summary:
        reruns = summary["reruns"]
        passed = sum(rerun["second"] == "pass" for rerun in reruns)
        lines.append(f"rerun {summary['rerun_run_id']}: {passed} of {len(reruns)} passed on rerun")
        lines += [f"  {r['test']}: first {r['first']}, second {r['second']}" for r in reruns]
    return "\n".join(lines)


def format_runs(
    runs_dir: Path, ref: str | None = None, days: int = 7, now: datetime | None = None
) -> str:
    """Return one line per run of the last `days` days, newest first, optionally of one ref."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    lines = []
    for meta_file in sorted(runs_dir.glob(f"*/{runs.RUN_META}"), reverse=True):
        run_dir = meta_file.parent
        # run ids start with their UTC creation time
        created = datetime.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        meta = json.loads(meta_file.read_text())
        if created < cutoff or (ref is not None and meta["ref"] != ref):
            continue
        summary_file = run_dir / "summary.json"
        summary = json.loads(summary_file.read_text()) if summary_file.exists() else None
        lines.append(_run_line(run_dir.name, meta, summary))
    return "\n".join(lines) or f"no runs in the last {days} days"


def _run_line(run_id: str, meta: dict[str, Any], summary: dict[str, Any] | None) -> str:
    """Id, ref, sha, purpose, exit, duration and rerun of one run; `-` for what is unknown."""
    purpose = meta.get("purpose") or "-"
    line = f"{run_id}  {meta['ref']}  {meta['sha'][:12]}  {purpose}"
    if summary is None:
        return f"{line}  exit -  -"
    exit_code = "boot-failed" if summary["exit_code"] is None else summary["exit_code"]
    line += f"  exit {exit_code}  {summary['duration_s']:.0f} s"
    return f"{line}  rerun {summary['rerun_run_id']}" if "rerun_run_id" in summary else line


def _area_lines(area: str, cases: dict[str, ET.Element]) -> list[str]:
    """Pass count of one area and a FAIL line per failed test."""
    outcomes = [runs.outcome(case) for case in cases.values()]
    skipped = outcomes.count("skip")
    line = f"area {area}: {outcomes.count('pass')}/{len(outcomes) - skipped} passed"
    lines = [f"{line}, {skipped} skipped" if skipped else line]
    for node_id, case in cases.items():
        if runs.outcome(case) == "fail":
            props = runs.properties(case)
            issues = props.get("failed_issues", props.get("issues", "-"))
            lines.append(f"  FAIL {node_id}  checkpoint {runs.checkpoint(case)}  issues {issues}")
    return lines


def _by_area(cases: dict[str, ET.Element]) -> dict[str, dict[str, ET.Element]]:
    """Group testcases by their `area` property, in JUnit order."""
    areas: dict[str, dict[str, ET.Element]] = {}
    for node_id, case in cases.items():
        areas.setdefault(runs.properties(case).get("area", NO_AREA), {})[node_id] = case
    return areas
