"""Known red list: loading, which entries apply to a test, and the xfail reason."""

from pathlib import Path

from scenarios.checks import known_red
from scenarios.checks.known_red import KnownRed

TOML = """
[[known_red]]
step = "next_drops_previous.next_heard"
role = "airplay-1"
since = "2026-10-30"
note = "libraop#60"

[[known_red]]
step = "plays_tone.starts"
account_set = "spotify-a"
since = "2026-11-01"
note = "region"

[[known_red]]
step = "volume.applied"
since = "2026-11-02"
note = "everywhere"
"""


def test_applying_filters_role_and_account_set(tmp_path: Path) -> None:
    """An entry applies when every filter it gives matches; one without filters always does."""
    path = tmp_path / "known_red.toml"
    path.write_text(TOML)
    entries = known_red.load(path)
    assert entries[0] == KnownRed(
        "next_drops_previous.next_heard", "2026-10-30", "libraop#60", role="airplay-1"
    )
    assert set(known_red.applying(entries, "airplay-1", None)) == {
        "next_drops_previous.next_heard",
        "volume.applied",
    }
    assert set(known_red.applying(entries, "dlna-1", "spotify-a")) == {
        "plays_tone.starts",
        "volume.applied",
    }


def test_real_list_loads() -> None:
    """The tracked list parses and every entry has a qualified step and a note."""
    entries = known_red.load(known_red.FILE)
    assert entries
    assert all("." in entry.step and entry.note for entry in entries)


def test_xfail_reason() -> None:
    """Each recorded known red joins the reason with its note; none gives no reason."""
    properties: list[tuple[str, object]] = [
        ("checkpoint", "a.b"),
        ("known_red", "a.b"),
        ("known_red_note", "bug 1"),
        ("known_red_error", "late"),
        ("known_red", "c.d"),
        ("known_red_note", "bug 2"),
    ]
    assert known_red.xfail_reason(properties) == "known red a.b: bug 1; known red c.d: bug 2"
    assert known_red.xfail_reason([("checkpoint", "a.b")]) == ""
