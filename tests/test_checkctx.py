"""CheckContext.step reporting of checkpoints and the issues they guard."""

import dataclasses
from typing import Any, cast

import pytest

from scenarios.checks.context import CheckContext, check
from scenarios.checks.known_red import KnownRed

KNOWN = KnownRed("plays_tone.starts", "2026-10-30", "upstream bug")


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


async def test_check_qualifies_its_steps() -> None:
    """A step inside a `@check` records `<check>.<step>`."""

    @check
    async def plays_tone(ctx: CheckContext, role: str) -> None:
        with ctx.step("starts"):
            pass

    ctx, recorded = _context()
    await plays_tone(ctx, "dlna-1")
    assert recorded == [("checkpoint", "plays_tone.starts")]


async def test_failing_known_red_ends_its_check() -> None:
    """A listed step that fails records the known red and ends only its check, after cleanup."""
    cleaned: list[str] = []

    @check
    async def plays_tone(ctx: CheckContext, role: str) -> None:
        try:
            with ctx.step("starts"):
                raise AssertionError("heard at 3.4 s\nassert 3.4 <= 3.0")
            with ctx.step("stops"):
                pass
        finally:
            cleaned.append(role)

    ctx, recorded = _context()
    await plays_tone(dataclasses.replace(ctx, known_red={KNOWN.step: KNOWN}), "airplay-1")
    assert cleaned == ["airplay-1"]
    assert recorded == [
        ("checkpoint", "plays_tone.starts"),
        ("known_red", "plays_tone.starts"),
        ("known_red_note", "upstream bug"),
        ("known_red_error", "heard at 3.4 s"),
    ]


async def test_unlisted_step_still_fails() -> None:
    """A step that is not listed fails its test while another step of its check is listed."""

    @check
    async def plays_tone(ctx: CheckContext, role: str) -> None:
        with ctx.step("stops"):
            raise AssertionError("still playing")

    ctx, recorded = _context()
    with pytest.raises(AssertionError, match="still playing"):
        await plays_tone(dataclasses.replace(ctx, known_red={KNOWN.step: KNOWN}), "airplay-1")
    assert ("failed_checkpoint", "plays_tone.stops") in recorded


async def test_passing_known_red_asks_for_removal() -> None:
    """A listed step that passes records `known_red_passed`."""

    @check
    async def plays_tone(ctx: CheckContext, role: str) -> None:
        with ctx.step("starts"):
            pass

    ctx, recorded = _context()
    await plays_tone(dataclasses.replace(ctx, known_red={KNOWN.step: KNOWN}), "airplay-1")
    assert recorded == [
        ("checkpoint", "plays_tone.starts"),
        ("known_red_passed", "plays_tone.starts"),
    ]


@pytest.mark.parametrize("error", [pytest.fail.Exception("never heard"), RuntimeError("no player")])
async def test_only_an_assertion_is_the_known_red(error: BaseException) -> None:
    """A `pytest.fail`, a timeout or an errored command in a listed step is a real failure."""

    @check
    async def plays_tone(ctx: CheckContext, role: str) -> None:
        with ctx.step("starts"):
            raise error

    ctx, recorded = _context()
    with pytest.raises(type(error)):
        await plays_tone(dataclasses.replace(ctx, known_red={KNOWN.step: KNOWN}), "airplay-1")
    assert ("failed_checkpoint", "plays_tone.starts") in recorded
    assert not any(name == "known_red" for name, _ in recorded)


def _context() -> tuple[CheckContext, list[tuple[str, object]]]:
    """Context with fake fixtures that records its properties."""
    recorded: list[tuple[str, object]] = []
    fake = cast(Any, None)
    ctx = CheckContext(fake, {}, fake, fake, fake, lambda key, value: recorded.append((key, value)))
    return ctx, recorded
