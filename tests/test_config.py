"""Config loader test."""

from pathlib import Path

import pytest

from labctl.config import REPO_ROOT, load_config, register_account, unregister_account


def test_example_config_loads() -> None:
    """The tracked example config is a valid LabConfig."""
    cfg = load_config(REPO_ROOT / "config" / "lab.example.toml")
    assert cfg.root == Path("/srv/lab")
    assert cfg.broker.capacity == 2
    assert cfg.runs_dir == Path("/srv/lab/runs")
    assert "nightly" in cfg.images
    assert sorted(cfg.devices) == ["homepod-1", "nestmini-1", "sonos-1", "wiim-1"]
    assert cfg.devices["sonos-1"].provider == "sonos"
    assert cfg.hardware is not None
    assert cfg.hardware.network == "lab-hw"


def test_device_id_with_player_kind_rejected(tmp_path: Path) -> None:
    """A device id must not look like a virtual player role."""
    path = tmp_path / "lab.toml"
    example = (REPO_ROOT / "config" / "lab.example.toml").read_text()
    path.write_text(example.replace("sonos-1 =", "sendspin-1 ="))
    with pytest.raises(ValueError, match="sendspin-1"):
        load_config(path)


def test_register_account(tmp_path: Path) -> None:
    """Registered sets land in [accounts] and every other line of the file stays."""
    example = (REPO_ROOT / "config" / "lab.example.toml").read_text()
    path = tmp_path / "lab.toml"
    path.write_text(example)
    spotify, apple = (
        Path("/srv/lab/accounts/spotify-a/data"),
        Path("/srv/lab/accounts/apple-a/data"),
    )
    register_account(path, "spotify-a", spotify)
    register_account(path, "apple-a", apple)
    cfg = load_config(path)
    assert cfg.accounts == {"spotify-a": spotify, "apple-a": apple}
    assert cfg.app_vars_file == Path("/srv/lab/secrets/app_vars.json")
    added = [line for line in path.read_text().splitlines() if line not in example.splitlines()]
    assert added == [f'spotify-a = "{spotify}"', f'apple-a = "{apple}"']


def test_unregister_account(tmp_path: Path) -> None:
    """Unregistering drops only that set's line."""
    example = (REPO_ROOT / "config" / "lab.example.toml").read_text()
    path = tmp_path / "lab.toml"
    path.write_text(example)
    apple = Path("/srv/lab/accounts/apple-a/data")
    register_account(path, "spotify-a", Path("/srv/lab/accounts/spotify-a/data"))
    register_account(path, "apple-a", apple)
    unregister_account(path, "spotify-a")
    assert load_config(path).accounts == {"apple-a": apple}
    removed = [line for line in example.splitlines() if line not in path.read_text().splitlines()]
    assert removed == []
