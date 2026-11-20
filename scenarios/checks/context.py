"""What a check needs from the run: client, players, tones, captures, events and reporting."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Concatenate, Protocol

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient

    from labctl.analyze import Capture, Manifest
    from scenarios.checks.known_red import KnownRed
    from scenarios.conftest import EventRecorder


class CaptureLoader(Protocol):
    """Loads a role's capture as written so far, from the test's start or from `since`."""

    def __call__(self, role: str, since: float | None = None) -> Capture:
        """Return the capture; pass `since` when polling, so each read stays short."""
        ...


@dataclasses.dataclass(frozen=True)
class CheckContext:
    """The fixtures of one test, bundled for checks shared between scenarios."""

    client: MusicAssistantClient
    players: dict[str, str]
    tones: Manifest
    capture: CaptureLoader
    events: EventRecorder
    record_property: Callable[[str, object], None]
    # the running check, set by `@check`; qualifies the checkpoint names of its steps
    check: str = ""
    # roles a test addressed, recorded once each as a `role` property
    used: set[str] = dataclasses.field(default_factory=set)
    # known reds that apply to this test, by qualified checkpoint
    known_red: Mapping[str, KnownRed] = dataclasses.field(default_factory=dict)

    @contextlib.asynccontextmanager
    async def reconnect(self, timeout: float) -> AsyncIterator[MusicAssistantClient]:
        """Yield a fresh client once the server runs again, such as after a restart."""
        from labctl.bootstrap import wait_running
        from scenarios.conftest import connect, lab_run

        run = lab_run()
        await wait_running(run.ma_url, timeout)
        async with connect(run, self.events) as client:
            yield client

    def player_id(self, role: str) -> str:
        """Return the player id of a role or leased device of the run."""
        # lab run keeps the captures of the roles a failed test used
        if role not in self.used:
            self.used.add(role)
            self.record_property("role", role)
        return self.players[role]

    def step(
        self, name: str, issues: tuple[str, ...] = ()
    ) -> contextlib.AbstractContextManager[None]:
        """Report a checkpoint `<check>.<name>` and the issues it guards, see `step`."""
        if not self.check:
            return step(self.record_property, name, issues)
        qualified = f"{self.check}.{name}"
        return step(self.record_property, qualified, issues, self.known_red.get(qualified))


type Check[**P] = Callable[Concatenate[CheckContext, P], Coroutine[Any, Any, None]]


class KnownRedError(Exception):
    """A known red step failed; it ends its check, not the test."""


def check[**P](fn: Check[P]) -> Check[P]:
    """Mark a check, so its steps carry its name; a failed known red step ends only the check."""

    @functools.wraps(fn)
    async def bound(ctx: CheckContext, /, *args: P.args, **kwargs: P.kwargs) -> None:
        with contextlib.suppress(KnownRedError):
            await fn(dataclasses.replace(ctx, check=fn.__name__), *args, **kwargs)

    return bound


@contextlib.contextmanager
def step(
    record_property: Callable[[str, object], None],
    name: str,
    issues: tuple[str, ...] = (),
    known: KnownRed | None = None,
) -> Iterator[None]:
    """Report a checkpoint and its issues, naming it on a failure; `known` marks a known red."""
    joined = " ".join(issues)
    record_property("checkpoint", name)
    if issues:
        record_property("issues", joined)
    try:
        yield
    # pytest.fail raises an OutcomeException, which is not an Exception
    except BaseException as exc:
        # only a failed assertion is the known red; an errored command or a time budget is not
        if known is not None and isinstance(exc, AssertionError):
            record_property("known_red", name)
            record_property("known_red_note", known.note)
            record_property("known_red_error", next(iter(str(exc).splitlines()), repr(exc)))
            raise KnownRedError(name) from exc
        record_property("failed_checkpoint", name)
        record_property("failed_issues", joined)
        exc.add_note(f"checkpoint {name}" + (f" ({joined})" if issues else ""))
        raise
    if known is not None:
        record_property("known_red_passed", name)
