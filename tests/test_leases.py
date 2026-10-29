"""Lease manager tests with a fake clock."""

import pytest

from labctl.leases import LeaseManager, device_resource, resource_names


class FakeClock:
    """Manually advanced clock."""

    def __init__(self) -> None:
        """Start at t=0."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


def test_acquire_and_queue_order() -> None:
    """Requests queue FIFO; a waiter on a busy account does not block one that can run."""
    manager = LeaseManager(2, {"account:a"}, default_ttl=120, clock=FakeClock())
    first = manager.request("agent-1", "dev", ["account:a"])
    blocked = manager.request("agent-2", "dev", ["account:a"])
    free = manager.request("agent-3", "dev")
    queued = manager.request("agent-4", "dev")

    assert manager.position(first.id) == 0
    assert manager.position(free.id) == 0
    assert manager.position(blocked.id) == 1
    assert manager.position(queued.id) == 2


def test_expiry_reclaims() -> None:
    """Heartbeats keep a lease; once they stop, the sweep frees it for the next waiter."""
    clock = FakeClock()
    manager = LeaseManager(1, set(), default_ttl=120, clock=clock)
    held = manager.request("agent-1", "dev")
    waiting = manager.request("agent-2", "dev")

    clock.now = 100
    manager.heartbeat(held.id)
    manager.heartbeat(waiting.id)
    clock.now = 200
    assert not manager.sweep()

    clock.now = 221
    manager.heartbeat(waiting.id)
    assert manager.sweep()
    assert [lease.id for lease in manager.leases] == [waiting.id]


def test_release_wakes_next_waiter() -> None:
    """Releasing a lease grants the head of the queue."""
    manager = LeaseManager(1, set(), default_ttl=120, clock=FakeClock())
    held = manager.request("agent-1", "dev")
    waiting = manager.request("agent-2", "dev")

    manager.release(held.id)

    assert manager.position(waiting.id) == 0
    assert manager.queue == []


def test_device_leases() -> None:
    """A busy device queues the next run for it; runs on other devices go ahead."""
    names = resource_names([], ["sonos-1", "wiim-1"])
    manager = LeaseManager(3, names, default_ttl=120, clock=FakeClock())
    first = manager.request("agent-1", "dev", [device_resource("sonos-1")])
    same = manager.request("agent-2", "dev", [device_resource("sonos-1")])
    other = manager.request("agent-3", "dev", [device_resource("wiim-1")])

    assert manager.position(first.id) == 0
    assert manager.position(same.id) == 1
    assert manager.position(other.id) == 0
    with pytest.raises(ValueError, match="device:nesthub-1"):
        manager.request("agent-4", "dev", [device_resource("nesthub-1")])
