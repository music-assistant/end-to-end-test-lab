"""Rendered systemd-networkd files of the host shim."""

from pathlib import Path

from labctl.config import REPO_ROOT, load_config
from labctl.hardware import networkd_files

NETDEV = """\
# Host-side macvlan shim so processes on the VM reach containers on the lab-hw macvlan network.
[NetDev]
Name=lab-shim
Kind=macvlan

[MACVLAN]
Mode=bridge
"""
NETWORK = """[Match]
Name=lab-shim

[Network]
Address=10.50.0.11/32

[Route]
Destination=10.50.0.32/27
"""
DROP_IN = """[Network]
MACVLAN=lab-shim
"""


def test_networkd_files() -> None:
    """The example [hardware] table renders the shim netdev, its network and the parent drop-in."""
    hw = load_config(REPO_ROOT / "config" / "lab.example.toml").hardware
    assert hw is not None
    assert networkd_files(hw) == {
        Path("/etc/systemd/network/25-lab-shim.netdev"): NETDEV,
        Path("/etc/systemd/network/25-lab-shim.network"): NETWORK,
        Path("/etc/systemd/network/10-netplan-eth0.network.d/lab-macvlan.conf"): DROP_IN,
    }
