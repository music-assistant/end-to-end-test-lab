"""Golden data dir of runs without an account set, and the per-run data dir seeded from it.

`golden/tier1/` holds `settings.json`, `auth.db` (lab admin user, JWT secret) and `token`
(a long-lived token for that user). Settings keys the lab sets in `settings.json`:

- `server_id`: fresh uuid4 hex per run
- `onboard_done`: true
- `default_providers_setup`: every DEFAULT_PROVIDERS domain of the ref, so none auto-create
- `core/webserver/values/bind_port`: 8095
- `core/streams/values/bind_port`: 8097
- `core/streams/values/volume_normalization_tracks`: "disabled"
- `core/discovery/values/zeroconf_interfaces`: "default"
"""

from __future__ import annotations

import ast
import asyncio
import json
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from music_assistant_client import MusicAssistantClient

from labctl import bootstrap, runs
from labctl.config import LabConfig

TOKEN_FILE = "token"
SETTINGS_FILE = "settings.json"
AUTH_DB = "auth.db"
LAB_USER = "lab"


def ensure_golden(config: LabConfig, worktree: Path, ma_image: str) -> Path:
    """Return the golden of runs without an account set, built on a throwaway boot when missing."""
    golden = config.golden_dir / "tier1"
    if (golden / TOKEN_FILE).exists():
        return golden
    build = config.golden_dir / f"tier1.build-{secrets.token_hex(3)}"
    data = build / "data"
    data.mkdir(parents=True)
    settings = seed_settings(worktree)
    (data / SETTINGS_FILE).write_text(json.dumps(settings, indent=2))
    token = _boot_and_create_admin(config, worktree, ma_image, data)
    runs.chown_to_me(build)
    shutil.copy2(data / AUTH_DB, build / AUTH_DB)
    (build / SETTINGS_FILE).write_text(json.dumps(settings, indent=2) + "\n")
    (build / TOKEN_FILE).write_text(token)
    shutil.rmtree(data)
    try:
        build.rename(golden)
    except OSError:
        # another run published its golden first
        if not (golden / TOKEN_FILE).exists():
            raise
        shutil.rmtree(build)
    return golden


def seed_settings(worktree: Path) -> dict[str, Any]:
    """Return the lab's settings.json for a ref (see the module docstring for the keys)."""
    return {
        "server_id": uuid.uuid4().hex,
        "onboard_done": True,
        "default_providers_setup": default_providers(worktree),
        "core": {
            "webserver": {"domain": "webserver", "values": {"bind_port": 8095}},
            "streams": {
                "domain": "streams",
                "values": {"bind_port": 8097, "volume_normalization_tracks": "disabled"},
            },
            "discovery": {"domain": "discovery", "values": {"zeroconf_interfaces": "default"}},
        },
    }


def default_providers(worktree: Path) -> list[str]:
    """Read the DEFAULT_PROVIDERS domains from the ref's constants.py."""
    tree = ast.parse((worktree / "music_assistant" / "constants.py").read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign | ast.Assign)
            and "DEFAULT_PROVIDERS" in {t.id for t in _targets(node) if isinstance(t, ast.Name)}
            and node.value is not None
        ):
            return sorted(domain for domain, _ in ast.literal_eval(node.value))
    msg = "DEFAULT_PROVIDERS not found in constants.py"
    raise RuntimeError(msg)


def prepare_data(golden: Path, worktree: Path, data_dir: Path) -> str:
    """Seed a run's data dir from the golden and return the API token."""
    shutil.copy2(golden / AUTH_DB, data_dir / AUTH_DB)
    settings = json.loads((golden / SETTINGS_FILE).read_text())
    settings["server_id"] = uuid.uuid4().hex
    # the golden may come from another ref
    settings["default_providers_setup"] = default_providers(worktree)
    (data_dir / SETTINGS_FILE).write_text(json.dumps(settings, indent=2))
    return (golden / TOKEN_FILE).read_text().strip()


def refresh_default_providers(data_dir: Path, worktree: Path) -> None:
    """Add the ref's DEFAULT_PROVIDERS to a data dir's settings so none auto-create at boot."""
    path = data_dir / SETTINGS_FILE
    settings = json.loads(path.read_text())
    known = settings.get("default_providers_setup", [])
    settings["default_providers_setup"] = sorted({*known, *default_providers(worktree)})
    path.write_text(json.dumps(settings, indent=2))


async def create_admin(url: str, password: str) -> str:
    """Create the lab admin through `/setup` once the server runs; return its session token."""
    await bootstrap.wait_running(url)
    async with aiohttp.ClientSession() as session:
        response = await session.post(
            f"{url}/setup", json={"username": LAB_USER, "password": password}
        )
        response.raise_for_status()
        token: str = (await response.json())["token"]
    return token


async def mint_token(url: str, session_token: str) -> str:
    """Mint a long-lived (365 day) token for the user behind a session token."""
    async with MusicAssistantClient(url, None, token=session_token) as client:
        token: str = await client.send_command("auth/token/create", name=LAB_USER)
    return token


def _boot_and_create_admin(config: LabConfig, worktree: Path, ma_image: str, data: Path) -> str:
    """Boot the server on `data`, create the lab admin and return a long-lived token."""
    container = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "-p",
            f"127.0.0.1::{runs.MA_PORT}",
            "-v",
            f"{worktree}:/src:ro",
            "-v",
            f"{data}:/data",
            "-v",
            f"{config.cache_dir / 'uv'}:/cache/uv",
            ma_image,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        port = subprocess.run(
            ["docker", "port", container, str(runs.MA_PORT)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        return asyncio.run(_create_admin(f"http://{port}"))
    finally:
        # a clean stop checkpoints auth.db's WAL into the file we keep
        subprocess.run(["docker", "stop", "-t", "30", container], check=True, capture_output=True)
        with (data.parent / "ma.log").open("w") as log:
            subprocess.run(["docker", "logs", container], check=True, stdout=log, stderr=log)
        subprocess.run(["docker", "rm", container], check=True, capture_output=True)


async def _create_admin(url: str) -> str:
    """Create the lab admin with a throwaway password and return a long-lived token."""
    return await mint_token(url, await create_admin(url, secrets.token_urlsafe(16)))


def _targets(node: ast.AnnAssign | ast.Assign) -> list[ast.expr]:
    """Return the assignment targets of a module-level statement."""
    return [node.target] if isinstance(node, ast.AnnAssign) else node.targets
