"""Lease bookkeeping for the broker: capacity slots plus named exclusive resources.

Every lease takes one capacity slot and may also name exclusive resources such as
`account:<set>` or `device:<id>`. Requests that cannot be granted wait in a FIFO queue;
waiters are granted in queue order, and a waiter blocked on a busy name does not hold up
later waiters whose resources are free. Leases and waiters expire unless heartbeated.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Lease:
    """A granted lease or a queued request for one."""

    id: str
    owner: str
    ref: str
    names: tuple[str, ...]
    ttl: float
    expires: float
    started: float | None = None

    @property
    def granted(self) -> bool:
        """Whether this lease holds its resources."""
        return self.started is not None


class LeaseManager:
    """Grant, queue, renew and expire leases against a fixed capacity."""

    def __init__(
        self,
        capacity: int,
        names: Iterable[str],
        default_ttl: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create an empty manager; `names` are the exclusive resources that may be leased."""
        self.capacity = capacity
        self._names = frozenset(names)
        self._default_ttl = default_ttl
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._queue: list[Lease] = []

    @property
    def leases(self) -> list[Lease]:
        """Granted leases."""
        return list(self._leases.values())

    @property
    def queue(self) -> list[Lease]:
        """Waiting requests in queue order."""
        return list(self._queue)

    def request(
        self, owner: str, ref: str, names: Iterable[str] = (), ttl: float | None = None
    ) -> Lease:
        """Queue a lease request and grant it right away when its resources are free."""
        wanted = tuple(names)
        unknown = set(wanted) - self._names
        if unknown:
            msg = f"unknown resources: {', '.join(sorted(unknown))}"
            raise ValueError(msg)
        lease = Lease(
            id=secrets.token_hex(4),
            owner=owner,
            ref=ref,
            names=wanted,
            ttl=ttl or self._default_ttl,
            expires=self._clock() + self._default_ttl,
        )
        self._queue.append(lease)
        self._grant()
        return lease

    def position(self, lease_id: str) -> int:
        """Return 0 for a granted lease, else the 1-based queue position."""
        if lease_id in self._leases:
            return 0
        return self._queue.index(self._get(lease_id)) + 1

    def heartbeat(self, lease_id: str) -> Lease:
        """Push back the expiry of a lease or a waiting request."""
        lease = self._get(lease_id)
        lease.expires = self._clock() + (lease.ttl if lease.granted else self._default_ttl)
        return lease

    def keep(self, lease_id: str, ttl: float) -> Lease:
        """Turn a lease into a hold that lasts `ttl` seconds from now without heartbeats."""
        lease = self._get(lease_id)
        lease.ttl = ttl
        lease.expires = self._clock() + ttl
        return lease

    def release(self, lease_id: str) -> None:
        """Drop a lease or waiting request and grant whoever can now run."""
        lease = self._get(lease_id)
        if lease.granted:
            del self._leases[lease_id]
        else:
            self._queue.remove(lease)
        self._grant()

    def sweep(self) -> bool:
        """Drop everything past its expiry; return whether anything changed."""
        now = self._clock()
        everything = [*self._leases.values(), *self._queue]
        expired = [lease.id for lease in everything if lease.expires <= now]
        for lease_id in expired:
            self.release(lease_id)
        return bool(expired)

    def to_dict(self) -> dict[str, Any]:
        """Serialize leases and queue for the state file."""
        return {
            "leases": [asdict(lease) for lease in self._leases.values()],
            "queue": [asdict(lease) for lease in self._queue],
        }

    def load(self, data: dict[str, Any]) -> None:
        """Replace the current state with a `to_dict` snapshot."""
        self._leases = {raw["id"]: _lease_from_dict(raw) for raw in data["leases"]}
        self._queue = [_lease_from_dict(raw) for raw in data["queue"]]
        self._grant()

    def _get(self, lease_id: str) -> Lease:
        """Look up a granted or queued lease."""
        if lease_id in self._leases:
            return self._leases[lease_id]
        for lease in self._queue:
            if lease.id == lease_id:
                return lease
        msg = f"unknown lease {lease_id}"
        raise KeyError(msg)

    def _grant(self) -> None:
        """Move queued requests whose resources are free into the granted set."""
        busy = {name for lease in self._leases.values() for name in lease.names}
        for lease in list(self._queue):
            if len(self._leases) >= self.capacity:
                break
            if busy.isdisjoint(lease.names):
                now = self._clock()
                self._queue.remove(lease)
                lease.started = now
                lease.expires = now + lease.ttl
                self._leases[lease.id] = lease
                busy.update(lease.names)


def resource_names(accounts: Iterable[str], devices: Iterable[str]) -> frozenset[str]:
    """Return every leasable exclusive resource: account sets and registered devices."""
    return frozenset(
        [*(account_resource(name) for name in accounts), *(device_resource(d) for d in devices)]
    )


def account_resource(name: str) -> str:
    """Return the lease resource name of an account set."""
    return f"account:{name}"


def device_resource(device_id: str) -> str:
    """Return the lease resource name of a registry device."""
    return f"device:{device_id}"


def _lease_from_dict(raw: dict[str, Any]) -> Lease:
    """Rebuild a lease from its state file form."""
    return Lease(**{**raw, "names": tuple(raw["names"])})
