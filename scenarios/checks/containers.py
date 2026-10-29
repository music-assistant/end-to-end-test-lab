"""Stop, start and restart a role's or service's container through the docker engine API."""

from __future__ import annotations

import os

import aiohttp

DOCKER_SOCKET = "/var/run/docker.sock"
# set on the tests container of a run without an account set only, together with the socket mount
PROJECT_ENV = "LAB_COMPOSE_PROJECT"
# a stop waits out the container's grace period before docker answers
DOCKER_TIMEOUT_S = 60.0


async def restart_role(role: str) -> None:
    """Restart the container of a role or compose service (`ma`) and return once it runs."""
    await _container_action(role, "restart")


async def stop_role(role: str) -> None:
    """Stop the role's container."""
    await _container_action(role, "stop")


async def start_role(role: str) -> None:
    """Start the role's container; a running container is left alone."""
    await _container_action(role, "start")


async def _container_action(role: str, action: str) -> None:
    """POST a container action to the docker engine for a role's or service's container."""
    # docker compose v2 names the first container of a service `<project>-<service>-1`
    name = f"{os.environ[PROJECT_ENV]}-{role}-1"
    timeout = aiohttp.ClientTimeout(total=DOCKER_TIMEOUT_S)
    async with (
        aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=DOCKER_SOCKET), timeout=timeout
        ) as session,
        session.post(f"http://localhost/containers/{name}/{action}") as response,
    ):
        # 304: the container already is in the wanted state
        if response.status >= 400:
            text = await response.text()
            raise AssertionError(f"docker {action} {name}: {response.status} {text}")
