"""Headless Sendspin player that captures received audio at its scheduled play-at time.

With `--visualizer` it is a visualizer-only client instead, like Hue Entertainment: no player
role, no audio and no capture.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import signal
from pathlib import Path

from aiohttp import ClientError
from aiosendspin.client import AudioFormat, SendspinClient
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerStatePayload,
)
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.trust_store import ClientPairingConfig, InMemoryClientPairingStore

from labctl.pacer import CaptureWriter

LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
SENDSPIN_PORT = 8927
RECONNECT_INTERVAL = 2.0
# About five seconds of 48 kHz 16-bit stereo; chunks are queued in memory, never dropped.
BUFFER_CAPACITY = 1024 * 1024
# what the Hue Entertainment bridge asks for: beats, peaks and a 17-bin mel spectrum at 20 Hz
VISUALIZER_BUFFER_CAPACITY = 2048
VISUALIZER_STATE = VisualizerStatePayload(
    types=["beat", "peak", "spectrum"],
    rate_max=20,
    spectrum=ClientHelloVisualizerSpectrum(n_disp_bins=17, scale="mel", f_min=20, f_max=20000),
)


class SendspinCapture:
    """Writes every audio chunk a Sendspin client receives at its play-at time."""

    def __init__(self, client: SendspinClient, writer: CaptureWriter) -> None:
        """Attach to the client's audio, clear and end events."""
        self._client = client
        self._writer = writer
        self._queue: asyncio.Queue[tuple[int, int, bytes]] = asyncio.Queue()
        # bumped on clear and end, so chunks queued or sleeping before it are discarded
        self._generation = 0
        client.add_audio_chunk_listener(self._on_audio_chunk)
        # player is the only role, so every clear or end concerns its audio
        client.add_stream_clear_listener(self._on_stop)
        client.add_stream_end_listener(self._on_stop)

    async def run(self) -> None:
        """Write queued chunks as their play-at time arrives, until cancelled."""
        while True:
            generation, server_timestamp_us, payload = await self._queue.get()
            if generation != self._generation:
                continue
            # converted at write time so the latest clock sync estimate applies
            delay_us = self._client.compute_play_time(server_timestamp_us) - self._client.now_us()
            if delay_us > 0:
                await asyncio.sleep(delay_us / 1e6)
            if generation == self._generation:
                self._writer.write(payload)

    def _on_audio_chunk(
        self, server_timestamp_us: int, payload: bytes, audio_format: AudioFormat, send_ahead: int
    ) -> None:
        """Queue a chunk with its server play-at timestamp."""
        self._queue.put_nowait((self._generation, server_timestamp_us, payload))

    def _on_stop(self, roles: list[str] | None) -> None:
        """Discard everything not yet written."""
        self._generation += 1
        while not self._queue.empty():
            self._queue.get_nowait()


def main(argv: list[str] | None = None) -> None:
    """Run the `lab-sendspin` CLI."""
    parser = argparse.ArgumentParser(prog="lab-sendspin", description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--dir", required=True, type=Path, help="captures directory")
    parser.add_argument("--server", default="ma", help="Music Assistant host")
    parser.add_argument("--identity", required=True, help="stable seed, <run_id>-<role>")
    parser.add_argument("--visualizer", action="store_true", help="visualizer role only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args.role, args.dir, args.server, args.identity, args.visualizer))


async def _run(
    role: str, captures_dir: Path, server: str, identity_seed: str, visualizer: bool
) -> None:
    """Connect, capture and keep reconnecting until SIGTERM or SIGINT."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    store = InMemoryClientPairingStore()
    await store.store_pairing_config(
        ClientPairingConfig(dynamic_pairing_code_enabled=False, unpaired_access_enabled=True)
    )
    identity = _identity(identity_seed)
    writer: CaptureWriter | None = None
    if visualizer:
        client = _visualizer_client(role, identity, store)
    else:
        writer = CaptureWriter(role, captures_dir, SAMPLE_RATE, CHANNELS)
        client = _player_client(role, identity, store)
    LOGGER.info("lab-%s has client id %s", role, client.identity.peer_id)
    tasks = [asyncio.create_task(_connect_loop(client, f"ws://{server}:{SENDSPIN_PORT}/sendspin"))]
    if writer is not None:
        tasks.append(asyncio.create_task(SendspinCapture(client, writer).run()))
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()
        if writer is not None:
            writer.close()


def _player_client(
    role: str, identity: Identity, store: InMemoryClientPairingStore
) -> SendspinClient:
    """Build a PCM-only player client."""
    return SendspinClient(
        identity,
        f"lab-{role}",
        [Roles.PLAYER],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[
                SupportedAudioFormat(
                    codec=AudioCodec.PCM,
                    channels=CHANNELS,
                    sample_rate=SAMPLE_RATE,
                    bit_depth=16,
                )
            ],
            buffer_capacity=BUFFER_CAPACITY,
            supported_commands=[],
        ),
    )


def _visualizer_client(
    role: str, identity: Identity, store: InMemoryClientPairingStore
) -> SendspinClient:
    """Build a client with the visualizer role only, as Hue Entertainment registers."""
    return SendspinClient(
        identity,
        f"lab-{role}",
        [Roles.VISUALIZER],
        pairing_store=store,
        visualizer_support=ClientHelloVisualizerSupport(buffer_capacity=VISUALIZER_BUFFER_CAPACITY),
        visualizer_state=VISUALIZER_STATE,
    )


async def _connect_loop(client: SendspinClient, url: str) -> None:
    """Keep the client connected, retrying while Music Assistant is down or restarting."""
    disconnected = asyncio.Event()
    client.add_disconnect_listener(disconnected.set)
    while True:
        disconnected.clear()
        try:
            await client.connect(url)
        except (
            ClientError,
            OSError,
            TimeoutError,
            HandshakeAbortedError,
            PairingError,
            RuntimeError,
        ) as err:
            LOGGER.debug("Could not connect to %s: %s", url, err)
            await asyncio.sleep(RECONNECT_INTERVAL)
            continue
        LOGGER.info("Connected to %s", url)
        await disconnected.wait()
        LOGGER.info("Disconnected from %s", url)
        await asyncio.sleep(RECONNECT_INTERVAL)


def _identity(seed: str) -> Identity:
    """Derive a stable Sendspin identity, so the player id is the same for every restart."""
    return Identity.from_private_bytes(hashlib.sha256(f"ma-lab-sendspin:{seed}".encode()).digest())
