"""Bring a booted run server to the state scenarios expect: music library and players."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from music_assistant_client import MusicAssistantClient

from labctl.devices import DEVICE_VOLUME, Device, matches
from labctl.runs import MPD_PORT, role_kind

READY_TIMEOUT_S = 180.0
PLAYERS_TIMEOUT_S = 60.0
SYNC_TIMEOUT_S = 60.0
POLL_S = 0.5
MUSIC_PATH = "/music"
# unity gain, so captures carry the tone level; several providers start players lower
PLAYER_VOLUME = 100
# native AirPlay 2 (PTP) stays silent with shairport-sync's nqptp; see docs/players.md
AIRPLAY_STREAMING_MODE = "ap2_compat"
# MA provider domain per player kind; sendspin is builtin
PROVIDER_DOMAINS = {
    "airplay": "airplay",
    "dlna": "dlna",
    "slim": "squeezelite",
    "snap": "snapcast",
    "mpd": "mpd",
}


async def wait_running(url: str, timeout: float = READY_TIMEOUT_S) -> None:
    """Wait until `/info` answers and reports the core as running (builtin providers loaded)."""
    async with aiohttp.ClientSession() as session, asyncio.timeout(timeout):
        while True:
            try:
                async with session.get(f"{url}/info") as response:
                    if response.ok and (await response.json())["status"] == "running":
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(POLL_S)


async def bootstrap(
    url: str,
    token: str,
    roles: tuple[str, ...],
    track_count: int | None,
    devices: dict[str, Device],
) -> None:
    """Add player providers, wait for every role and device; tier 1 (`track_count`) adds tones."""
    await wait_running(url)
    async with MusicAssistantClient(url, None, token=token) as client:
        # providers first, so discovery runs while the library syncs
        await _add_player_providers(client, roles, devices)
        if track_count is not None:
            instance_id = await _add_music_folder(client)
            await client.send_command("music/sync", providers=[instance_id])
            await _wait_track_count(client, track_count)
        await _name_mpd_players(client, roles)
        players = await _wait_players(client, roles, devices)
        await _prepare_players(client, players, roles)


async def _add_player_providers(
    client: MusicAssistantClient, roles: tuple[str, ...], devices: dict[str, Device]
) -> None:
    """Set up the provider of every role kind and device in the run; MPD gets its hosts listed."""
    domains = {PROVIDER_DOMAINS[kind] for kind in map(role_kind, roles) if kind in PROVIDER_DOMAINS}
    domains |= {device.provider for device in devices.values()}
    # a tier 2 golden keeps the providers earlier runs added
    existing = {
        conf["domain"]: conf["instance_id"]
        for conf in await client.send_command("config/providers")
    }
    for domain in sorted(domains):
        instance_id = existing.get(domain) or await _setup_provider(client, domain)
        if domain == PROVIDER_DOMAINS["mpd"]:
            # MPD has no discovery: one manual entry per role, by compose service name
            hosts = [f"{role}:{MPD_PORT}" for role in roles if role_kind(role) == "mpd"]
            await client.send_command(
                "config/providers/save",
                provider_domain=domain,
                values={"manual_discovery_ip_addresses": hosts},
                instance_id=instance_id,
            )


async def _setup_provider(client: MusicAssistantClient, domain: str) -> str:
    """Add a provider that needs no setup values and return its instance id."""
    step = await client.send_command("config/providers/setup", provider_domain=domain)
    if step["type"] != "finish":
        msg = f"{domain} setup did not finish: {step}"
        raise RuntimeError(msg)
    instance_id: str = step["result"]["instance_id"]
    return instance_id


async def _name_mpd_players(client: MusicAssistantClient, roles: tuple[str, ...]) -> None:
    """Rename MPD players to `lab-<role>`; the provider names them after the host."""
    wanted = {f"mpd_{role}_{MPD_PORT}": f"lab-{role}" for role in roles if role_kind(role) == "mpd"}
    try:
        async with asyncio.timeout(PLAYERS_TIMEOUT_S):
            while wanted:
                players: list[dict[str, Any]] = await client.send_command("players/all")
                for player_id in wanted.keys() & {p["player_id"] for p in players}:
                    await client.send_command(
                        "config/players/save",
                        player_id=player_id,
                        values={"name": wanted.pop(player_id)},
                    )
                await asyncio.sleep(POLL_S)
    except TimeoutError:
        msg = f"MPD players not registered after {PLAYERS_TIMEOUT_S:.0f} s: {sorted(wanted)}"
        raise RuntimeError(msg) from None


async def _add_music_folder(client: MusicAssistantClient) -> str:
    """Set up filesystem_local on the tone library through its setup flow."""
    step = await client.send_command("config/providers/setup", provider_domain="filesystem_local")
    step = await client.send_command(
        "config/flows/submit",
        flow_id=step["flow_id"],
        values={"content_type": "music", "path": MUSIC_PATH},
    )
    if step["type"] != "finish":
        msg = f"filesystem_local setup did not finish: {step}"
        raise RuntimeError(msg)
    instance_id: str = step["result"]["instance_id"]
    return instance_id


async def _wait_track_count(client: MusicAssistantClient, expected: int) -> None:
    """Wait until the library holds exactly the tone manifest's tracks."""
    count = 0
    try:
        async with asyncio.timeout(SYNC_TIMEOUT_S):
            while (count := await client.send_command("music/tracks/count")) != expected:
                await asyncio.sleep(POLL_S)
    except TimeoutError:
        msg = f"library has {count} tracks after {SYNC_TIMEOUT_S:.0f} s, expected {expected}"
        raise RuntimeError(msg) from None


async def _wait_players(
    client: MusicAssistantClient, roles: tuple[str, ...], devices: dict[str, Device]
) -> dict[str, dict[str, Any]]:
    """Wait until every role (`lab-<role>`) and device is an available player; return them by id."""
    wanted = len(roles) + len(devices)
    found: dict[str, dict[str, Any]] = {}
    try:
        async with asyncio.timeout(PLAYERS_TIMEOUT_S):
            while True:
                players: list[dict[str, Any]] = await client.send_command("players/all")
                available = [p for p in players if p["available"]]
                found = {role: p for role in roles for p in available if p["name"] == f"lab-{role}"}
                found |= {
                    device_id: p
                    for device_id, device in devices.items()
                    for p in available
                    if matches(
                        device,
                        p["name"],
                        p["provider"],
                        [o["protocol_domain"] for o in p["output_protocols"]],
                    )
                }
                if len(found) == wanted:
                    return found
                await asyncio.sleep(POLL_S)
    except TimeoutError:
        missing = sorted({*roles, *devices} - set(found))
        msg = f"players missing after {PLAYERS_TIMEOUT_S:.0f} s: {missing}"
        raise RuntimeError(msg) from None


async def _prepare_players(
    client: MusicAssistantClient, players: dict[str, dict[str, Any]], roles: tuple[str, ...]
) -> None:
    """Set roles to unity volume and devices to a low one; pin the AirPlay roles' streaming mode."""
    for player_key, player in players.items():
        if "volume_set" in player["supported_features"]:
            volume = PLAYER_VOLUME if player_key in roles else DEVICE_VOLUME
            await client.send_command(
                "players/cmd/volume_set", player_id=player["player_id"], volume_level=volume
            )
        # a real AirPlay device such as a HomePod keeps native AirPlay 2
        if player_key not in roles or role_kind(player_key) != "airplay":
            continue
        # the setting lives on the AirPlay protocol player behind the universal player
        [protocol_id] = [
            output["output_protocol_id"]
            for output in player["output_protocols"]
            if output["protocol_domain"] == "airplay"
        ]
        await client.send_command(
            "config/players/save",
            player_id=protocol_id,
            values={"streaming_mode": AIRPLAY_STREAMING_MODE},
        )
