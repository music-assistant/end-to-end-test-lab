"""Ref resolution, worktrees and base image choice against a throwaway upstream."""

import subprocess
from pathlib import Path

import pytest

from labctl import refs

IMAGES = {"nightly": "server:nightly", "stable": "server:stable"}


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return stdout."""
    cmd = ["git", "-C", str(repo), "-c", "user.name=lab", "-c", "user.email=lab@test", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, branch: str, name: str) -> str:
    """Commit a file named `name` on `branch` and return the sha."""
    git(repo, "checkout", "-q", branch)
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def shas(tmp_path: Path) -> dict[str, str]:
    """Upstream where stable forked from dev, plus a feature branch and a backport PR."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "dev")
    git(upstream, "commit", "-q", "--allow-empty", "-m", "base")
    git(upstream, "branch", "stable")
    commit(upstream, "stable", "stable_fix")
    shas = {"dev_change": commit(upstream, "dev", "dev_change")}
    git(upstream, "branch", "feature")
    shas["feature"] = commit(upstream, "feature", "feature")
    git(upstream, "branch", "backport", "stable")
    shas["backport"] = commit(upstream, "backport", "backport")
    git(upstream, "update-ref", "refs/pull/7/head", shas["backport"])
    shas["stable"] = commit(upstream, "stable", "stable_later")
    shas["dev"] = commit(upstream, "dev", "dev_later")
    refs.sync_mirror(str(upstream), tmp_path / "server.git")
    return shas


def test_resolve(tmp_path: Path, shas: dict[str, str]) -> None:
    """Branches, shas and pr/<n> resolve to full shas."""
    mirror = tmp_path / "server.git"
    assert refs.resolve(mirror, "dev") == shas["dev"]
    assert refs.resolve(mirror, "stable") == shas["stable"]
    assert refs.resolve(mirror, "feature") == shas["feature"]
    assert refs.resolve(mirror, shas["dev_change"][:10]) == shas["dev_change"]
    assert refs.resolve(mirror, "pr/7") == shas["backport"]


def test_worktree(tmp_path: Path, shas: dict[str, str]) -> None:
    """A worktree per sha is checked out at that sha and reused."""
    mirror = tmp_path / "server.git"
    path = refs.ensure_worktree(mirror, tmp_path / "refs", shas["feature"])
    assert path == tmp_path / "refs" / shas["feature"]
    assert git(path, "rev-parse", "HEAD") == shas["feature"]
    assert (path / "feature").exists()
    assert refs.ensure_worktree(mirror, tmp_path / "refs", shas["feature"]) == path


def test_base_image(tmp_path: Path, shas: dict[str, str]) -> None:
    """Stable and branches forked from it get the stable image, the rest nightly."""
    mirror = tmp_path / "server.git"
    assert refs.base_image(mirror, shas["stable"], IMAGES) == "server:stable"
    assert refs.base_image(mirror, shas["backport"], IMAGES) == "server:stable"
    assert refs.base_image(mirror, shas["dev"], IMAGES) == "server:nightly"
    assert refs.base_image(mirror, shas["feature"], IMAGES) == "server:nightly"
