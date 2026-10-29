"""What a check needs from the run: client, players, tones, captures, events and reporting."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient

    from labctl.analyze import Capture, Manifest
    from scenarios.conftest import EventRecorder


class CaptureLoader(Protocol):
    """Loads a role's capture as written so far, from the test's start or from `since`."""

    def __call__(self, role: str, since: float | None = None) -> Capture:
        """Return the capture; pass `since` when polling, so each read stays short."""
        ...


@dataclass(frozen=True)
class CheckContext:
    """The fixtures of one test, bundled for checks shared between scenarios."""

    client: MusicAssistantClient
    players: dict[str, str]
    tones: Manifest
    capture: CaptureLoader
    events: EventRecorder
    record_property: Callable[[str, object], None]

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
        return self.players[role]

    @contextlib.contextmanager
    def step(self, name: str, issues: tuple[str, ...] = ()) -> Iterator[None]:
        """Report a checkpoint and the issues it guards, and name it on the error when it fails."""
        joined = " ".join(issues)
        self.record_property("checkpoint", name)
        if issues:
            self.record_property("issues", joined)
        try:
            yield
        # pytest.fail raises an OutcomeException, which is not an Exception
        except BaseException as exc:
            self.record_property("failed_checkpoint", name)
            self.record_property("failed_issues", joined)
            exc.add_note(f"checkpoint {name}" + (f" ({joined})" if issues else ""))
            raise
