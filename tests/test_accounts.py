"""Tests for what freezing an account set keeps of its login instance's settings."""

from typing import Any

from labctl.accounts import _reset_login_settings


def _settings(webserver: dict[str, Any], streams: dict[str, Any]) -> dict[str, Any]:
    return {"core": {"webserver": {"values": webserver}, "streams": {"values": streams}}}


def test_host_network_login_ports_are_dropped() -> None:
    """A host-network login on a second port must not pin that port into the golden."""
    settings = _settings(
        {"base_url": "http://10.50.0.10:8096", "bind_port": 8096, "log_level": "info"},
        {"bind_port": 8097},
    )
    _reset_login_settings(settings)
    assert settings == _settings({"log_level": "info"}, {})


def test_bridge_login_keeps_default_ports() -> None:
    """A bridge login only pinned the base URL; the ports were never set."""
    settings = _settings({"base_url": "http://10.50.0.10:8095"}, {})
    _reset_login_settings(settings)
    assert settings == _settings({}, {})
