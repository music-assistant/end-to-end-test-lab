"""Music providers come back logged in after a reload and still find tracks (#6119).

A server restart would also take down the tests container's network, which music runs share
with the server, so this reloads every music provider instead. That covers the stored credentials
and token refresh of a provider start, not the server's own startup.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import MediaType, ProviderType

if TYPE_CHECKING:
    from music_assistant_client import MusicAssistantClient

pytestmark = [pytest.mark.music, pytest.mark.timeout(180)]

AVAILABLE_TIMEOUT_S = 60.0
SEARCH_QUERY = "The Beatles"
SEARCH_LIMIT = 5
POLL_S = 1.0


async def test_provider_reload(
    client: MusicAssistantClient, record_testsuite_property: Callable[[str, object], None]
) -> None:
    """Every streaming provider reloads, is available within 60 s and returns search results."""
    reloaded = {
        provider.instance_id: provider.domain
        for provider in client.providers
        if provider.type == ProviderType.MUSIC
        and not client.get_provider_manifest(provider.domain).builtin
    }
    if not reloaded:
        pytest.skip("this golden has no streaming provider")
    record_testsuite_property("auth_restart providers", " ".join(reloaded.values()))

    started = time.time()
    for instance_id in reloaded:
        await client.config.reload_provider(instance_id)
    deadline = time.monotonic() + AVAILABLE_TIMEOUT_S
    while missing := await _unavailable(client, reloaded):
        assert time.monotonic() < deadline, (
            f"not available {AVAILABLE_TIMEOUT_S:.0f} s after a reload: {missing}"
        )
        await asyncio.sleep(POLL_S)
    record_testsuite_property("auth_restart available s", f"{time.time() - started:.1f}")

    found = await client.music.search(SEARCH_QUERY, [MediaType.TRACK], limit=SEARCH_LIMIT)
    domains = {
        mapping.provider_domain
        for track in found.tracks
        if track.provider != "library"
        for mapping in track.provider_mappings
    }
    silent = sorted(set(reloaded.values()) - domains)
    assert not silent, f"no {SEARCH_QUERY!r} tracks after a reload from: {silent}"


async def _unavailable(client: MusicAssistantClient, providers: dict[str, str]) -> list[str]:
    """Domains of the given provider instances that are not loaded and available."""
    loaded = {p["instance_id"]: p["available"] for p in await client.send_command("providers")}
    return sorted(domain for instance, domain in providers.items() if not loaded.get(instance))
