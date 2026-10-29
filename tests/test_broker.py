"""Broker round trip over a real Unix socket."""

import asyncio
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from labctl.broker import Broker
from labctl.broker_client import BrokerClient, BrokerError
from labctl.config import BrokerConfig


@pytest.fixture
def config() -> Iterator[BrokerConfig]:
    """Broker config in a short temp dir; macOS caps Unix socket paths at 104 bytes."""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        yield BrokerConfig(
            socket=Path(tmp) / "labd.sock",
            state_file=Path(tmp) / "state.json",
            capacity=1,
            lease_ttl_s=120,
            heartbeat_s=30,
        )


async def test_queue_release_and_restart(config: BrokerConfig) -> None:
    """The second acquire queues, gets granted on release, and survives a restart."""
    broker = Broker(config, frozenset())
    await broker.start()
    client = BrokerClient(config.socket, config.heartbeat_s)

    first = await client.call("acquire", owner="agent-1", ref="dev", names=[])
    second = await client.call("acquire", owner="agent-2", ref="dev", names=[])
    assert first["position"] == 0
    assert second["position"] == 1

    waiting = asyncio.create_task(client.call("wait", id=second["id"], timeout=5))
    await asyncio.sleep(0.1)
    await client.release(first["id"])
    assert (await asyncio.wait_for(waiting, 1))["position"] == 0

    await broker.stop()
    restarted = Broker(config, frozenset())
    await restarted.start()
    status = await client.status()
    await restarted.stop()
    assert [lease["id"] for lease in status["leases"]] == [second["id"]]


@pytest.fixture
def broker_thread(config: BrokerConfig) -> Iterator[None]:
    """Run labd on its own loop in a thread, for the synchronous lease API."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    broker = Broker(config, frozenset())
    asyncio.run_coroutine_threadsafe(broker.start(), loop).result()
    yield
    asyncio.run_coroutine_threadsafe(broker.stop(), loop).result()
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def test_lease_lost_during_run(config: BrokerConfig, broker_thread: None) -> None:
    """A lease labd dropped mid-run fails the block once it finishes."""
    client = BrokerClient(config.socket, heartbeat_s=0.05)
    finished = False
    with pytest.raises(BrokerError, match="was lost during the run"):
        with client.lease_sync("agent", "dev") as lease:
            asyncio.run(client.release(lease.id))
            time.sleep(0.2)
            finished = True
    assert finished


def test_kept_lease_becomes_hold(config: BrokerConfig, broker_thread: None) -> None:
    """A kept lease stays granted with the keep ttl after the block until released."""
    client = BrokerClient(config.socket, config.heartbeat_s)
    with client.lease_sync("agent", "dev") as lease:
        lease.keep_ttl = 3600
    [held] = asyncio.run(client.status())["leases"]
    assert held["id"] == lease.id
    assert held["expires"] - time.time() > 3000
    asyncio.run(client.release(lease.id))
    assert asyncio.run(client.status())["leases"] == []
