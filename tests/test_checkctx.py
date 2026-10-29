"""CheckContext.step reporting of checkpoints and the issues they guard."""

from typing import Any, cast

import pytest

from scenarios.checks.context import CheckContext


def test_passing_step_records_checkpoint() -> None:
    """A passing step records its name and issues."""
    ctx, recorded = _context()
    with ctx.step("plays", ("#1", "support#2")):
        pass
    assert recorded == [("checkpoint", "plays"), ("issues", "#1 support#2")]


def test_failing_step_notes_checkpoint() -> None:
    """A failing step records the failed checkpoint and adds it as a note to the same error."""
    ctx, recorded = _context()
    error = AssertionError("silent")
    with pytest.raises(AssertionError) as raised, ctx.step("plays", ("#1", "#2")):
        raise error
    assert raised.value is error
    assert error.__notes__ == ["checkpoint plays (#1 #2)"]
    assert recorded[2:] == [("failed_checkpoint", "plays"), ("failed_issues", "#1 #2")]


def test_step_without_issues() -> None:
    """A step without issues records no issues property."""
    ctx, recorded = _context()
    with ctx.step("plays"):
        pass
    assert recorded == [("checkpoint", "plays")]


def _context() -> tuple[CheckContext, list[tuple[str, object]]]:
    """Context with fake fixtures that records its properties."""
    recorded: list[tuple[str, object]] = []
    fake = cast(Any, None)
    ctx = CheckContext(fake, {}, fake, fake, fake, lambda key, value: recorded.append((key, value)))
    return ctx, recorded
