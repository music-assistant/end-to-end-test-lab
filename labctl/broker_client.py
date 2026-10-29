"""Client for labd: acquire with queue feedback, heartbeat while held, release."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Long enough to ride out a labd restart by systemd.
RETRY_S = 15.0
RETRY_INTERVAL_S = 0.5


class BrokerError(Exception):
    """labd rejected a request."""


@dataclass
class HeldLease:
    """A lease held by `BrokerClient.lease_sync`."""

    id: str
    # set inside the block to turn the lease into a hold of this many seconds instead of
    # releasing it on exit
    keep_ttl: float | None = None


class BrokerClient:
    """Talk to labd over its Unix socket, one connection per request."""

    def __init__(self, socket_path: Path, heartbeat_s: float) -> None:
        """Create a client; `heartbeat_s` paces heartbeats and queue polls."""
        self._socket = socket_path
        self._heartbeat_s = heartbeat_s

    async def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return the reply, raising BrokerError on failure."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RETRY_S
        while True:
            try:
                reply = await self._roundtrip({"op": op, **fields})
                break
            except (OSError, asyncio.IncompleteReadError):
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(RETRY_INTERVAL_S)
        if not reply.pop("ok"):
            raise BrokerError(reply["error"])
        return reply

    async def acquire(
        self,
        owner: str,
        ref: str,
        names: Iterable[str] = (),
        on_queued: Callable[[int], None] | None = None,
    ) -> str:
        """Block until granted and return the lease id."""
        reply = await self.call("acquire", owner=owner, ref=ref, names=list(names))
        lease_id: str = reply["id"]
        position = 0
        try:
            while reply["position"] > 0:
                if on_queued is not None and reply["position"] != position:
                    on_queued(reply["position"])
                position = reply["position"]
                reply = await self.call("wait", id=lease_id, timeout=self._heartbeat_s)
        except BaseException:
            # Interrupted while queued: free our place instead of blocking others until expiry.
            await self.release(lease_id)
            raise
        return lease_id

    async def release(self, lease_id: str) -> None:
        """Release a lease or a queued request."""
        await self.call("release", id=lease_id)

    async def status(self) -> dict[str, Any]:
        """Return capacity, granted leases and the queue."""
        return await self.call("status")

    @contextlib.contextmanager
    def lease_sync(
        self,
        owner: str,
        ref: str,
        names: Iterable[str] = (),
        on_queued: Callable[[int], None] | None = None,
    ) -> Iterator[HeldLease]:
        """Hold a lease around a synchronous block while a helper thread heartbeats it.

        Raises BrokerError after the block when labd dropped the lease in the meantime.
        """
        lease = HeldLease(asyncio.run(self.acquire(owner, ref, names, on_queued=on_queued)))
        stop = threading.Event()
        lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_thread, args=(lease.id, stop, lost), daemon=True
        )
        heartbeat.start()
        try:
            yield lease
        finally:
            stop.set()
            heartbeat.join()
            if not lost.is_set():
                try:
                    if lease.keep_ttl is None:
                        asyncio.run(self.release(lease.id))
                    else:
                        asyncio.run(self.call("keep", id=lease.id, ttl=lease.keep_ttl))
                except BrokerError as err:
                    if not _is_unknown(err, lease.id):
                        raise
                    lost.set()
        if lost.is_set():
            msg = f"lease {lease.id} was lost during the run"
            raise BrokerError(msg)

    def _heartbeat_thread(
        self, lease_id: str, stop: threading.Event, lost: threading.Event
    ) -> None:
        """Renew a lease every interval until stopped; only an unknown lease ends it early."""
        while not stop.wait(self._heartbeat_s):
            try:
                asyncio.run(self.call("heartbeat", id=lease_id))
            except BrokerError as err:
                if _is_unknown(err, lease_id):
                    lost.set()
                    return
            except (OSError, asyncio.IncompleteReadError):
                # labd unreachable for longer than RETRY_S: try again next interval
                continue

    async def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON line and read one back."""
        reader, writer = await asyncio.open_unix_connection(self._socket)
        with contextlib.closing(writer):
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readuntil(b"\n")
        reply: dict[str, Any] = json.loads(line)
        return reply


def _is_unknown(err: BrokerError, lease_id: str) -> bool:
    """Return whether labd no longer knows the lease (expired and swept, or released)."""
    return str(err) == f"unknown lease {lease_id}"
