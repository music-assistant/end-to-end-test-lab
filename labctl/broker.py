"""labd: the lease broker, a JSON-lines server on a Unix socket.

Each request is one JSON object per line with an `op` field; each reply is one line with
`ok` plus either result fields or `error`. Ops:

- `acquire {owner, ref, names}` and `hold {owner, ref, names, ttl}` queue a request and reply
  `{id, position}`; position 0 means granted, n >= 1 is the place in the queue.
- `wait {id, timeout}` long-polls: it renews the request and replies with the new
  position after the next state change or after `timeout` seconds. Clients loop on it
  while queued, so a broker restart only drops one poll and the queue survives.
- `heartbeat {id}` renews a lease, `release {id}` drops it, `status` lists leases and queue.
- `keep {id, ttl}` turns a lease into a hold of `ttl` seconds in place, so a run kept up
  after `lab run` returns keeps its slot and account without a queue round trip.

State is written to `state_file` (temp file + rename) after every change and reloaded
on start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from labctl.config import BrokerConfig, load_config
from labctl.leases import LeaseManager, resource_names

SWEEP_INTERVAL_S = 1.0


class Broker:
    """Serve the lease protocol on a Unix socket and persist state."""

    def __init__(
        self,
        config: BrokerConfig,
        names: frozenset[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Prepare a broker; call `start` to load state and listen."""
        self._config = config
        self._manager = LeaseManager(config.capacity, names, config.lease_ttl_s, clock)
        self._changed = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._sweeper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Reload persisted state and start listening."""
        if self._config.state_file.exists():
            self._manager.load(json.loads(self._config.state_file.read_text()))
        self._server = await asyncio.start_unix_server(self._handle, path=self._config.socket)
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        """Stop listening and sweeping."""
        assert self._server is not None and self._sweeper is not None
        self._sweeper.cancel()
        self._server.close()
        await self._server.wait_closed()

    async def serve_forever(self) -> None:
        """Start and run until cancelled."""
        await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer every request line on one connection."""
        with contextlib.closing(writer):
            while line := await reader.readline():
                try:
                    reply = {"ok": True, **await self._dispatch(json.loads(line))}
                except (KeyError, ValueError) as err:
                    reply = {"ok": False, "error": str(err.args[0] if err.args else err)}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        """Run one request against the lease manager."""
        op = req["op"]
        if op == "status":
            return {"capacity": self._manager.capacity, **self._manager.to_dict()}
        if op == "wait":
            return await self._wait(req["id"], float(req["timeout"]))
        if op == "heartbeat":
            expires = self._manager.heartbeat(req["id"]).expires
            self._save()
            return {"expires": expires}
        if op == "keep":
            expires = self._manager.keep(req["id"], float(req["ttl"])).expires
            self._save()
            return {"expires": expires}
        if op == "release":
            self._manager.release(req["id"])
            result: dict[str, Any] = {}
        elif op in ("acquire", "hold"):
            lease = self._manager.request(
                owner=req["owner"],
                ref=req["ref"],
                names=req.get("names", ()),
                ttl=float(req["ttl"]) if op == "hold" else None,
            )
            result = {"id": lease.id, "position": self._manager.position(lease.id)}
        else:
            msg = f"unknown op {op}"
            raise ValueError(msg)
        self._save()
        self._notify()
        return result

    async def _wait(self, lease_id: str, timeout: float) -> dict[str, Any]:
        """Renew a request and reply once its position may have changed."""
        self._manager.heartbeat(lease_id)
        self._save()
        if self._manager.position(lease_id) > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
        return {"id": lease_id, "position": self._manager.position(lease_id)}

    async def _sweep_loop(self) -> None:
        """Expire leases and waiters that stopped heartbeating."""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            if self._manager.sweep():
                self._save()
                self._notify()

    def _save(self) -> None:
        """Persist state atomically."""
        tmp = self._config.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._manager.to_dict(), indent=2))
        os.replace(tmp, self._config.state_file)

    def _notify(self) -> None:
        """Wake long-polling waiters so they report their new position."""
        self._changed.set()
        self._changed = asyncio.Event()


def main() -> None:
    """Run labd with the lab config."""
    config = load_config()
    broker = Broker(config.broker, resource_names(config.accounts, config.devices))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(broker.serve_forever())
