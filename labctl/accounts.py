"""Account sets for music runs: a login instance per set, frozen into a golden data dir.

`accounts/<set>/` on the VM holds:

- `data/`: the golden data dir, mounted in place at /data by music runs
- `password`: password of the `lab` admin, for logging in to the login instance's UI
- `token`: long-lived API token of the `lab` admin, written by freeze
- `compose.yml` and `run.json` (sha, ref, url): the login instance of `init` or `reopen`,
  removed by freeze
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from labctl import bootstrap, refs, runs, seed
from labctl.config import LabConfig, register_account, unregister_account
from labctl.refs import Checkout

SET_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
PASSWORD_FILE = "password"
COMPOSE_FILE = "compose.yml"
FIRST_LOGIN_PORT = 8095
LOGIN_PORTS = 100
STOP_TIMEOUT_S = 30


@dataclass(frozen=True)
class LoginInstance:
    """Where and as whom to log in to a set's login instance."""

    url: str
    username: str
    password: str
    sha: str


@dataclass(frozen=True)
class SetStatus:
    """One account set: its login instance URL while that runs, and whether it is frozen."""

    name: str
    login_url: str | None
    frozen: bool


def init(config: LabConfig, name: str, ref: str, host_network: bool = False) -> LoginInstance:
    """Create a fresh data dir for an account set and start its login instance on the VLAN.

    With `host_network` the server shares the VM's network stack, so a phone on VLAN 50 can
    discover it over mDNS (Soloist pairing); the web and stream ports are then picked free.
    """
    _check_name(name)
    directory = config.accounts_dir / name
    with refs.checkout(config, ref) as checkout:
        if directory.exists():
            msg = f"account set {name} already exists at {directory}"
            raise RuntimeError(msg)
        (directory / "data").mkdir(parents=True)
        try:
            url = _write_login(
                config, name, checkout, host_network, seed.seed_settings(checkout.worktree)
            )
        except BaseException:
            # nothing has started yet, so a retried init starts clean
            shutil.rmtree(directory)
            raise
    _compose(directory, name, "up", "-d", "--quiet-pull")
    password = secrets.token_urlsafe(12)
    asyncio.run(seed.create_admin(url, password))
    (directory / PASSWORD_FILE).write_text(password + "\n")
    return LoginInstance(url, seed.LAB_USER, password, checkout.sha)


def reopen(
    config: LabConfig,
    name: str,
    ref: str,
    leased: Callable[[], bool],
    host_network: bool = False,
) -> LoginInstance:
    """Start a login instance on an existing set's golden; `freeze` then mints a new token.

    `leased` reports whether a run holds or waits for the set; reopen refuses if so.
    """
    _check_name(name)
    directory = config.accounts_dir / name
    settings_file = directory / "data" / seed.SETTINGS_FILE
    if not settings_file.exists():
        msg = f"account set {name} has no data dir at {settings_file.parent}"
        raise RuntimeError(msg)
    # a run.json without a running container is a failed reopen, which may be retried
    if _login_url(config, name) is not None:
        msg = f"account set {name} already has a login instance"
        raise RuntimeError(msg)
    settings = json.loads(settings_file.read_text())
    with refs.checkout(config, ref) as checkout:
        # a run checks for run.json after taking the lease, and _write_login writes it first
        if leased():
            msg = f"account:{name} is leased or queued, try again after the run"
            raise RuntimeError(msg)
        try:
            url = _write_login(config, name, checkout, host_network, settings)
        except BaseException:
            (directory / runs.RUN_META).unlink(missing_ok=True)
            raise
    _compose(directory, name, "up", "-d", "--quiet-pull")
    asyncio.run(bootstrap.wait_running(url))
    password = (directory / PASSWORD_FILE).read_text().strip()
    return LoginInstance(url, seed.LAB_USER, password, checkout.sha)


def is_open(config: LabConfig, name: str) -> bool:
    """Return whether a set has a login instance, so its golden must not be mounted by a run."""
    return (config.accounts_dir / name / runs.RUN_META).exists()


def remove(config: LabConfig, name: str, config_file: Path) -> None:
    """Stop a set's login instance if any, delete its directory and drop it from lab.toml."""
    _check_name(name)
    directory = config.accounts_dir / name
    if not directory.exists() and name not in config.accounts:
        msg = f"no account set {name}"
        raise RuntimeError(msg)
    if is_open(config, name):
        _compose(directory, name, "down")
    if directory.exists():
        runs.chown_to_me(directory)
        shutil.rmtree(directory)
    if name in config.accounts:
        unregister_account(config_file, name)


def statuses(config: LabConfig) -> list[SetStatus]:
    """Return every account set on disk or in lab.toml."""
    on_disk = (
        {path.name for path in config.accounts_dir.iterdir() if path.is_dir()}
        if config.accounts_dir.exists()
        else set()
    )
    return [
        SetStatus(name, _login_url(config, name), name in config.accounts)
        for name in sorted(on_disk | config.accounts.keys())
    ]


def freeze(config: LabConfig, name: str, config_file: Path) -> None:
    """Mint the set's token, stop its login instance, clean the data dir and register the set."""
    directory = config.accounts_dir / name
    meta = directory / runs.RUN_META
    if not meta.exists():
        msg = f"account set {name} has no login instance"
        raise RuntimeError(msg)
    password = (directory / PASSWORD_FILE).read_text().strip()
    token = asyncio.run(_login_and_mint(json.loads(meta.read_text())["url"], password))
    # a clean stop checkpoints the databases' WAL into the files the golden keeps
    _compose(directory, name, "stop", "-t", str(STOP_TIMEOUT_S))
    _compose(directory, name, "down")
    runs.chown_to_me(directory)
    data = directory / "data"
    # the pem pair is the server's remote access identity
    for pem in data.glob("webrtc_*.pem"):
        pem.unlink()
    settings = json.loads((data / seed.SETTINGS_FILE).read_text())
    _reset_login_settings(settings)
    (data / seed.SETTINGS_FILE).write_text(json.dumps(settings, indent=2))
    (directory / seed.TOKEN_FILE).write_text(token)
    meta.unlink()
    (directory / COMPOSE_FILE).unlink()
    if name not in config.accounts:
        register_account(config_file, name, data)


def token(config: LabConfig, name: str) -> str:
    """Return the API token of a frozen account set."""
    return (config.accounts[name].parent / seed.TOKEN_FILE).read_text().strip()


def _write_login(
    config: LabConfig, name: str, checkout: Checkout, host_network: bool, settings: dict[str, Any]
) -> str:
    """Write run.json, build the server image, write settings and compose file, return the URL."""
    directory = config.accounts_dir / name
    meta = directory / runs.RUN_META
    meta.write_text(json.dumps({"sha": checkout.sha, "ref": checkout.ref}) + "\n")
    cliairplay = refs.fetch_cliairplay(checkout.worktree, config.cache_dir)
    runs.prepare_worktree(checkout.worktree, cliairplay)
    ma_image, _ = runs.build_ma_image(checkout.image)
    data = directory / "data"
    # host networking binds on every interface, so the ports must be free VM-wide
    port = _free_port("" if host_network else config.host)
    url = f"http://{config.host}:{port}"
    ma = runs.ma_service(
        config, ma_image, checkout.worktree, data, f"{config.host}:{port}:{runs.MA_PORT}"
    )
    project: dict[str, Any] = {"name": _project(name)}
    if host_network:
        del ma["ports"]
        ma["network_mode"] = "host"
        settings["core"]["webserver"]["values"]["bind_port"] = port
        settings["core"]["streams"]["values"]["bind_port"] = _free_port("", port + 1)
    else:
        project["networks"] = {"default": {"name": _project(name)}}
    project["services"] = {"ma": {**runs.with_app_vars(config, ma), "restart": "unless-stopped"}}
    if not host_network:
        # unreachable by service name from a host-network server; only YT Music needs it
        project["services"][runs.POTOKEN_SERVICE] = {
            **runs.potoken_service(config),
            "restart": "unless-stopped",
        }
    # OAuth sign-ins return the browser to base_url, whose auto value is the container's own IP
    settings["core"]["webserver"]["values"]["base_url"] = url
    (data / seed.SETTINGS_FILE).write_text(json.dumps(settings, indent=2))
    meta.write_text(json.dumps({"sha": checkout.sha, "ref": checkout.ref, "url": url}) + "\n")
    (directory / COMPOSE_FILE).write_text(yaml.safe_dump(project, sort_keys=False))
    return url


def _reset_login_settings(settings: dict[str, Any]) -> None:
    """Drop what a login instance pinned in `settings`, so runs boot the set on the defaults."""
    # back to auto: inside a run the server lives on the run's bridge
    settings["core"]["webserver"]["values"].pop("base_url", None)
    # a host-network login binds the ports it found free; runs reach the server on MA_PORT
    for section in ("webserver", "streams"):
        settings["core"][section]["values"].pop("bind_port", None)


async def _login_and_mint(url: str, password: str) -> str:
    """Log in as the lab admin and mint a long-lived token."""
    async with aiohttp.ClientSession() as session:
        response = await session.post(
            f"{url}/auth/login",
            json={"credentials": {"username": seed.LAB_USER, "password": password}},
        )
        response.raise_for_status()
        session_token: str = (await response.json())["token"]
    return await seed.mint_token(url, session_token)


def _login_url(config: LabConfig, name: str) -> str | None:
    """Return the login instance URL when its container runs."""
    directory = config.accounts_dir / name
    meta = directory / runs.RUN_META
    if not meta.exists() or not _compose(directory, name, "ps", "-q").strip():
        return None
    url: str = json.loads(meta.read_text())["url"]
    return url


def _free_port(host: str, start: int = FIRST_LOGIN_PORT) -> int:
    """Return the first port from `start` up that nothing listens on at `host`."""
    for port in range(start, FIRST_LOGIN_PORT + LOGIN_PORTS):
        with socket.socket() as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    msg = f"no free login port on {host}"
    raise RuntimeError(msg)


def _check_name(name: str) -> None:
    """Reject set names that are not a plain directory name."""
    if not SET_NAME.fullmatch(name):
        msg = f"account set name {name!r} must be lowercase letters, digits and dashes"
        raise ValueError(msg)


def _project(name: str) -> str:
    """Compose project and network name of a set's login instance."""
    return f"lab-acct-{name}"


def _compose(directory: Path, name: str, *args: str) -> str:
    """Run docker compose against a set's login instance and return stdout."""
    cmd = ["docker", "compose", "-p", _project(name), "-f", str(directory / COMPOSE_FILE), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
