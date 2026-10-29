"""Load the lab configuration from config/lab.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from labctl.devices import Device

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "lab.toml"
# virtual player kinds a run starts as `<kind>-<n>` roles
PLAYER_KINDS = ("sendspin", "airplay", "dlna", "slim", "snap", "mpd")


@dataclass(frozen=True)
class BrokerConfig:
    """Lease broker settings."""

    socket: Path
    state_file: Path
    capacity: int
    lease_ttl_s: float
    heartbeat_s: float
    keep_ttl_s: float = 14400


@dataclass(frozen=True)
class HardwareConfig:
    """The shared macvlan network device runs join, and the VM's shim address on it."""

    network: str
    parent: str
    subnet: str
    gateway: str
    ip_range: str
    shim: str


@dataclass(frozen=True)
class LabConfig:
    """Everything labctl needs to know about this lab installation."""

    root: Path
    broker: BrokerConfig
    mirror: Path
    upstream: str
    images: dict[str, str]
    accounts: dict[str, Path] = field(default_factory=dict)
    app_vars_file: Path | None = None
    host: str = "127.0.0.1"
    devices: dict[str, Device] = field(default_factory=dict)
    hardware: HardwareConfig | None = None

    @property
    def runs_dir(self) -> Path:
        """Directory holding one subdirectory per run id."""
        return self.root / "runs"

    @property
    def refs_dir(self) -> Path:
        """Directory holding one git worktree per checked out sha."""
        return self.root / "refs"

    @property
    def golden_dir(self) -> Path:
        """Seed data dirs copied into fresh tier 1 runs."""
        return self.root / "golden"

    @property
    def accounts_dir(self) -> Path:
        """One directory per tier 2 account set: golden data dir, token, login instance."""
        return self.root / "accounts"

    @property
    def music_dir(self) -> Path:
        """Generated tone library, bind-mounted read-only at /music/tones."""
        return self.root / "music" / "tones"

    @property
    def cache_dir(self) -> Path:
        """Shared caches: uv wheels, cliairplay binaries."""
        return self.root / "cache"

    @property
    def flakes_file(self) -> Path:
        """Flake ledger: one JSON line per rerun test of a gate run."""
        return self.root / "flakes.jsonl"


def config_path() -> Path:
    """Return the config file in use; `LAB_CONFIG` overrides the default location."""
    return Path(os.environ.get("LAB_CONFIG", DEFAULT_CONFIG))


def load_config(path: Path | None = None) -> LabConfig:
    """Read the TOML config from `path` or `config_path()`."""
    with (path or config_path()).open("rb") as fh:
        raw = tomllib.load(fh)
    broker = raw["broker"]
    secrets = raw.get("secrets", {})
    hardware = raw.get("hardware")
    return LabConfig(
        root=Path(raw["vm"]["root"]),
        broker=BrokerConfig(
            socket=Path(broker["socket"]),
            state_file=Path(broker["state_file"]),
            capacity=int(broker["capacity"]),
            lease_ttl_s=float(broker["lease_ttl_s"]),
            heartbeat_s=float(broker["heartbeat_s"]),
            keep_ttl_s=float(broker.get("keep_ttl_s", BrokerConfig.keep_ttl_s)),
        ),
        mirror=Path(raw["refs"]["mirror"]),
        upstream=raw["refs"]["upstream"],
        images=dict(raw["images"]),
        accounts={name: Path(p) for name, p in raw.get("accounts", {}).items()},
        app_vars_file=Path(secrets["app_vars_file"]) if "app_vars_file" in secrets else None,
        host=raw["vm"]["host"],
        devices=_devices(raw.get("devices", {})),
        hardware=HardwareConfig(**hardware) if hardware is not None else None,
    )


def register_account(config_file: Path, name: str, data_dir: Path) -> None:
    """Add `<name> = "<data_dir>"` at the end of the [accounts] table, keeping everything else."""
    lines = config_file.read_text().splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if line.strip() == "[accounts]"]
    if not headers:
        msg = f"no [accounts] table in {config_file}"
        raise RuntimeError(msg)
    end = headers[0] + 1
    for i in range(end, len(lines)):
        if lines[i].startswith("["):
            break
        if lines[i].strip():
            end = i + 1
    lines.insert(end, f'{name} = "{data_dir}"\n')
    config_file.write_text("".join(lines))


def unregister_account(config_file: Path, name: str) -> None:
    """Remove the `<name> = ...` line from the [accounts] table, keeping everything else."""
    lines = config_file.read_text().splitlines(keepends=True)
    in_accounts = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            in_accounts = line.strip() == "[accounts]"
        elif in_accounts and line.split("=", 1)[0].strip() == name:
            del lines[i]
            config_file.write_text("".join(lines))
            return
    msg = f"no account {name} in {config_file}"
    raise RuntimeError(msg)


def _devices(raw: dict[str, dict[str, Any]]) -> dict[str, Device]:
    """Registry entries of the [devices] table; ids must not look like virtual player roles."""
    for device_id in raw:
        if device_id.rsplit("-", 1)[0] in PLAYER_KINDS:
            msg = f"device id {device_id!r} uses a virtual player kind, pick another prefix"
            raise ValueError(msg)
    # entries may carry more keys than the ones a Device needs
    return {device_id: Device(entry["provider"], entry["name"]) for device_id, entry in raw.items()}
