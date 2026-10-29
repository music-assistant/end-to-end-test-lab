"""Server log helpers of the scenarios."""

from pathlib import Path

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
