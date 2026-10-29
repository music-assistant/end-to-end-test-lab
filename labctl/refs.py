"""Server checkouts: bare mirror, one worktree per sha, base image choice, cliairplay."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import platform
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from labctl.config import LabConfig

CLIAIRPLAY_RELEASES = "https://github.com/music-assistant/airplay-cli/releases/download"


@dataclass(frozen=True)
class Checkout:
    """A ref resolved to a sha, its worktree and the base image to run it on."""

    ref: str
    sha: str
    worktree: Path
    image: str


@contextlib.contextmanager
def checkout(config: LabConfig, ref: str) -> Iterator[Checkout]:
    """Sync the mirror, resolve `ref`, make sure its worktree exists and yield the checkout.

    The refs lock is held for the block: write the `run.json` that records the sha inside it,
    so `lab gc` cannot remove the worktree before it is in use.
    """
    # concurrent runs share the mirror and may want the same new worktree
    with locked(config):
        sync_mirror(config.upstream, config.mirror)
        sha = resolve(config.mirror, ref)
        worktree = ensure_worktree(config.mirror, config.refs_dir, sha)
        yield Checkout(
            ref=ref,
            sha=sha,
            worktree=worktree,
            image=base_image(config.mirror, sha, config.images),
        )


@contextlib.contextmanager
def locked(config: LabConfig) -> Iterator[None]:
    """Hold the exclusive refs lock for the block."""
    path = config.refs_dir / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sync_mirror(upstream: str, mirror: Path) -> None:
    """Clone the bare mirror on first use, fetch it afterwards."""
    # A --mirror clone fetches refs/* so refs/pull/<n>/head comes along.
    if mirror.exists():
        _git(mirror, "fetch", "--prune", "origin")
    else:
        _git(None, "clone", "--mirror", upstream, str(mirror))


def resolve(mirror: Path, ref: str) -> str:
    """Resolve `dev`, `stable`, a branch, a sha or `pr/<n>` to a full sha."""
    if match := re.fullmatch(r"pr/(\d+)", ref):
        ref = f"refs/pull/{match[1]}/head"
    return _git(mirror, "rev-parse", "--verify", f"{ref}^{{commit}}")


def ensure_worktree(mirror: Path, refs_dir: Path, sha: str) -> Path:
    """Return the detached worktree for `sha`, creating it when missing."""
    path = refs_dir / sha
    if not path.exists():
        _git(mirror, "worktree", "add", "--detach", str(path), sha)
    return path


def base_image(mirror: Path, sha: str, images: dict[str, str]) -> str:
    """Pick `images.stable` for commits on or forked from stable, else `images.nightly`."""
    if _is_ancestor(mirror, sha, "stable"):
        return images["stable"]
    # Stable only receives cherry-picks, so a branch forked from stable shares a newer
    # merge-base with stable than with dev.
    fork_stable = _git(mirror, "merge-base", sha, "stable")
    fork_dev = _git(mirror, "merge-base", sha, "dev")
    if fork_stable != fork_dev and _is_ancestor(mirror, fork_dev, fork_stable):
        return images["stable"]
    return images["nightly"]


def remove_worktrees(mirror: Path, refs_dir: Path, keep: set[str]) -> list[Path]:
    """Prune stale worktree metadata and remove every worktree whose sha is not in `keep`."""
    _git(mirror, "worktree", "prune")
    removed = [path for path in refs_dir.iterdir() if path.is_dir() and path.name not in keep]
    for path in removed:
        _git(mirror, "worktree", "remove", "--force", str(path))
    return removed


def fetch_cliairplay(worktree: Path, cache_dir: Path) -> Path:
    """Download the cliairplay binary pinned in the ref's Dockerfile, once per version."""
    # Mirrors scripts/fetch_airplay_cli.py; that script needs Python 3.14 and writes into
    # the checkout, so its logic is repeated here against the shared cache.
    dockerfile = (worktree / "Dockerfile").read_text()
    version = _docker_arg(dockerfile, "CLIAIRPLAY_VERSION")
    manifest_sha = _docker_arg(dockerfile, "CLIAIRPLAY_CHECKSUMS_SHA256")
    asset = f"cliairplay-linux-{_arch()}"
    destination = cache_dir / "cliairplay" / version / asset
    if destination.exists():
        return destination

    manifest = _download(f"{CLIAIRPLAY_RELEASES}/{version}/SHA256SUMS", manifest_sha)
    binary = _download(f"{CLIAIRPLAY_RELEASES}/{version}/{asset}", _asset_sha(manifest, asset))
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{asset}.{os.getpid()}")
    tmp.write_bytes(binary)
    tmp.chmod(0o755)
    tmp.replace(destination)
    return destination


def _git(repo: Path | None, *args: str) -> str:
    """Run git (in `repo` when given) and return stripped stdout."""
    cmd = ["git", *(["-C", str(repo)] if repo else []), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def _is_ancestor(mirror: Path, ancestor: str, descendant: str) -> bool:
    """Return whether `ancestor` is reachable from `descendant`."""
    cmd = ["git", "-C", str(mirror), "merge-base", "--is-ancestor", ancestor, descendant]
    return subprocess.run(cmd, check=False).returncode == 0


def _docker_arg(dockerfile: str, name: str) -> str:
    """Read a pinned `ARG name=value` from Dockerfile text."""
    match = re.search(rf"^ARG\s+{re.escape(name)}=(\S+)\s*$", dockerfile, flags=re.MULTILINE)
    if match is None:
        msg = f"{name} not pinned in Dockerfile"
        raise RuntimeError(msg)
    return match[1]


def _arch() -> str:
    """Return the cliairplay architecture name of this host."""
    return "aarch64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64"


def _asset_sha(manifest: bytes, asset: str) -> str:
    """Return the checksum listed for `asset` in a SHA256SUMS file."""
    for line in manifest.decode().splitlines():
        digest, _, name = line.partition(" ")
        if name.strip().lstrip("*") == asset:
            return digest
    msg = f"{asset} missing from SHA256SUMS"
    raise RuntimeError(msg)


def _download(url: str, sha256: str) -> bytes:
    """Download `url` and check its sha256."""
    request = Request(url, headers={"User-Agent": "ma-lab"})  # noqa: S310
    with urlopen(request, timeout=60) as response:  # noqa: S310
        content: bytes = response.read()
    if hashlib.sha256(content).hexdigest() != sha256:
        msg = f"checksum mismatch for {url}"
        raise RuntimeError(msg)
    return content
