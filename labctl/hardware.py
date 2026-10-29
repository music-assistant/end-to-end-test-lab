"""VM side of device runs: the macvlan network on the LAN and the host shim that reaches it."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from labctl.config import HardwareConfig, LabConfig

# host-side macvlan interface: a macvlan container cannot reach its host's parent address
SHIM_LINK = "lab-shim"
NETWORKD_DIR = Path("/etc/systemd/network")


def networkd_files(hw: HardwareConfig) -> dict[Path, str]:
    """Return the systemd-networkd files that create the shim and attach it to the parent."""
    return {
        NETWORKD_DIR / f"25-{SHIM_LINK}.netdev": (
            "# Host-side macvlan shim so processes on the VM reach containers on the"
            f" {hw.network} macvlan network.\n"
            "[NetDev]\n"
            f"Name={SHIM_LINK}\n"
            "Kind=macvlan\n"
            "\n"
            "[MACVLAN]\n"
            "Mode=bridge\n"
        ),
        NETWORKD_DIR / f"25-{SHIM_LINK}.network": (
            "[Match]\n"
            f"Name={SHIM_LINK}\n"
            "\n"
            "[Network]\n"
            f"Address={hw.shim}/32\n"
            "\n"
            "[Route]\n"
            f"Destination={hw.ip_range}\n"
        ),
        # netplan generates the parent's network file, so the shim is attached by a drop-in
        NETWORKD_DIR / f"10-netplan-{hw.parent}.network.d" / "lab-macvlan.conf": (
            f"[Network]\nMACVLAN={SHIM_LINK}\n"
        ),
    }


def install(config: LabConfig) -> list[str]:
    """Install the shim files and create the docker network where missing; return what changed."""
    hw = require(config)
    changed = []
    for path, content in networkd_files(hw).items():
        if path.exists() and path.read_text() == content:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=path.suffix) as tmp:
            tmp.write(content)
            tmp.flush()
            _sudo("install", "-D", "-m", "644", tmp.name, str(path))
        changed.append(f"wrote {path}")
    if changed:
        _sudo("networkctl", "reload")
    if _run("docker", "network", "inspect", hw.network).returncode != 0:
        subprocess.run(
            [
                "docker",
                "network",
                "create",
                "-d",
                "macvlan",
                "--subnet",
                hw.subnet,
                "--gateway",
                hw.gateway,
                "--ip-range",
                hw.ip_range,
                "--aux-address",
                f"shim={hw.shim}",
                "-o",
                f"parent={hw.parent}",
                hw.network,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        changed.append(f"created docker network {hw.network}")
    return changed


def check(config: LabConfig) -> list[str]:
    """Return the failed checks of the shim and the docker network; empty when both are ready."""
    hw = require(config)
    failed = []
    link = _run("ip", "-br", "link", "show", SHIM_LINK)
    if link.returncode != 0:
        failed.append(f"shim link {SHIM_LINK} missing")
    elif "UP" not in link.stdout.split():
        failed.append(f"shim link {SHIM_LINK} is not up: {link.stdout.strip()}")
    network = _run("docker", "network", "inspect", "-f", '{{index .Options "parent"}}', hw.network)
    if network.returncode != 0:
        failed.append(f"docker network {hw.network} missing")
    elif (parent := network.stdout.strip()) != hw.parent:
        failed.append(f"docker network {hw.network} has parent {parent}, expected {hw.parent}")
    return failed


def require(config: LabConfig) -> HardwareConfig:
    """Return the [hardware] table; raises RuntimeError when lab.toml has none."""
    if config.hardware is None:
        msg = "no [hardware] table in lab.toml"
        raise RuntimeError(msg)
    return config.hardware


def _sudo(*args: str) -> None:
    """Run a command as root without a password prompt; raises CalledProcessError on failure."""
    subprocess.run(["sudo", "-n", *args], check=True, capture_output=True, text=True)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a command for its output and exit code."""
    return subprocess.run(list(args), check=False, capture_output=True, text=True)
