"""Server log and track picker helpers of the scenarios."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scenarios import helpers

BEFORE = "2026-10-27 09:12:00.001 ERROR (MainThread) [music_assistant] old\n"
AFTER = (
    "2026-10-27 09:12:01.123 ERROR (MainThread) [music_assistant] boom\n"
    "2026-10-27 09:12:01.200 INFO (MainThread) [music_assistant] saw ERROR in a message\n"
    "2026-10-27 09:12:01.300 CRITICAL (MainThread) [music_assistant] down\n"
)


def test_ma_log_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ERROR and CRITICAL lines after the offset count, a message mentioning ERROR does not."""
    log = tmp_path / "musicassistant.log"
    log.write_text(BEFORE)
    monkeypatch.setattr(helpers, "MA_LOG", log)
    offset = helpers.ma_log_size()
    with log.open("a") as file:
        file.write(AFTER)
    assert helpers.ma_log_errors(offset) == [
        "2026-10-27 09:12:01.123 ERROR (MainThread) [music_assistant] boom",
        "2026-10-27 09:12:01.300 CRITICAL (MainThread) [music_assistant] down",
    ]


async def test_pick_tracks_searches_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search that returns nothing yet is asked again, as MA keeps a slow provider searching."""
    track = SimpleNamespace(
        uri="spotify://track/1",
        provider="spotify",
        duration=180,
        provider_mappings=[SimpleNamespace(available=True)],
    )
    results = iter([[], [track]])

    async def search(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(tracks=next(results))

    async def get_library_tracks(**_kwargs: object) -> list[object]:
        return []

    client = cast(
        Any,
        SimpleNamespace(
            music=SimpleNamespace(search=search, get_library_tracks=get_library_tracks)
        ),
    )
    monkeypatch.setattr(helpers, "SEARCH_RETRY_S", 0)
    assert await helpers.pick_tracks(client, 1) == [track]
