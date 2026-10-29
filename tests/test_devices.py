"""Device registry entries and how players resolve to them."""

import pytest

from labctl.devices import Device, matches

WIIM = Device("wiim", "WiiM Ultra")


@pytest.mark.parametrize(
    ("name", "provider", "protocols", "expected"),
    [
        ("WiiM Ultra", "wiim", [], True),
        ("WiiM Ultra", "wiim--x1y2", [], True),
        ("WiiM Ultra", "universal_player--x1y2", ["airplay", "wiim"], True),
        ("Kantoor", "wiim--x1y2", ["wiim"], False),
    ],
)
def test_matches(name: str, provider: str, protocols: list[str], expected: bool) -> None:
    """Name must equal; provider matches as domain, instance id or output protocol domain."""
    assert matches(WIIM, name, provider, protocols) is expected
