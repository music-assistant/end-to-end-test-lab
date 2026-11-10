"""Run lifecycle: run directory, compose project, tests container, teardown and summary."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from labctl import hardware
from labctl.config import PLAYER_KINDS, REPO_ROOT, LabConfig
from labctl.devices import to_env
from labctl.refs import Checkout

RUN_META = "run.json"
MA_PORT = 8095
PLAYERS_IMAGE = "lab/players:latest"
POTOKEN_SERVICE = "potoken"
RADIO_SERVICE = "radio"
RADIO_PORT = 8000
STOP_TIMEOUT_S = 10
# watchdog over the whole tests container; a deep run takes about 45 min
TESTS_TIMEOUT_S = 2 * 3600
CLIAIRPLAY_DIR = "music_assistant/providers/airplay/bin"
# every non-Sendspin player writes 48 kHz 16-bit stereo into this FIFO; its pacer reads it
FIFO = "/fifo/pcm"
PCM_BYTES_PER_SECOND = 48000 * 2 * 2
# players that write as fast as the FIFO drains; their pacer drains it at the PCM rate
UNPACED_KINDS = ("slim", "mpd")
MPD_PORT = 6600
APP_VARS_PATH = "/secrets/app_vars.json"
# where scenarios see the server's data dir
TESTS_DATA_DIR = "/data"
# where the tests container sees the run's copy of the lab repo
TESTS_SRC_DIR = "/src"
DOCKER_SOCKET = "/var/run/docker.sock"
# without an account set: the tone library and the run's writable music folder, below /music
MUSIC_PATH = "/music"
TONES_MOUNT = f"{MUSIC_PATH}/tones"
EXTRA_MOUNT = f"{MUSIC_PATH}/extra"
# what a run mounts from the lab repo, copied per run so a sync of the repo cannot change it
# mid-run; the tests container imports labctl from /src and reads pytest's config there
RUN_SRC = (
    "labctl",
    "scenarios",
    "pyproject.toml",
    "tones/__init__.py",
    "tones/generate.py",
    "tones/manifest.json",
    "images/players/mpd.conf",
    "images/shairport/shairport-sync.conf",
)


@dataclass(frozen=True)
class Selection:
    """Which scenarios a run's pytest collects: a marker expression, -k, explicit node ids."""

    marker: str
    test_expr: str | None = None
    node_ids: tuple[str, ...] = ()

    def pytest_command(self) -> list[str]:
        """Return the tests container's pytest command line."""
        command = ["pytest", *(self.node_ids or (f"{TESTS_SRC_DIR}/scenarios",))]
        command += ["-p", "no:cacheprovider", "-m", self.marker]
        if self.test_expr:
            command += ["-k", self.test_expr]
        return [*command, "--junitxml", "/run/junit.xml"]


@dataclass(frozen=True)
class Run:
    """One run: its id, directory on the VM, the player roles it starts and its data dir."""

    id: str
    dir: Path
    roles: tuple[str, ...]
    # the account set's golden data dir, mounted in place instead of a fresh one
    account_data: Path | None = None
    # gate areas the run was started with, empty outside a gate run
    areas: tuple[str, ...] = ()
    deep: bool = False
    # registry ids of the real devices the run leased
    devices: tuple[str, ...] = ()
    # what the run is for, as `lab runs` shows it
    purpose: str = ""

    @property
    def hardware(self) -> bool:
        """Whether the run joins the hardware network to reach real devices."""
        return bool(self.devices)

    @property
    def identity_scope(self) -> str:
        """Seed of the players' identities: the account set in a music run, else the run id."""
        # a music run writes into the golden, which then keeps one player config per role
        return self.id if self.account_data is None else self.account_data.parent.name

    @property
    def project(self) -> str:
        """Compose project name, also the name of the run's private network."""
        return f"lab-{self.id}"

    @property
    def data_dir(self) -> Path:
        """Music Assistant data dir, mounted at /data."""
        return self.account_data or self.dir / "data"

    @property
    def captures_dir(self) -> Path:
        """Pacer captures of every role."""
        return self.dir / "captures"

    @property
    def music_extra_dir(self) -> Path:
        """Music folder scenarios add files to, at /music/extra in the server."""
        return self.dir / "music-extra"

    @property
    def src_dir(self) -> Path:
        """The run's copy of the lab repo files it mounts (RUN_SRC)."""
        return self.dir / "src"

    @property
    def compose_file(self) -> Path:
        """Rendered compose project."""
        return self.dir / "compose.yml"


def parse_roles(players: str) -> tuple[str, ...]:
    """Turn `sendspin,sendspin` into numbered roles `sendspin-1`, `sendspin-2`."""
    counts: dict[str, int] = {}
    roles = []
    for kind in (part.strip() for part in players.split(",")):
        if kind not in PLAYER_KINDS:
            msg = f"unknown player kind {kind!r}, known: {', '.join(PLAYER_KINDS)}"
            raise ValueError(msg)
        counts[kind] = counts.get(kind, 0) + 1
        roles.append(f"{kind}-{counts[kind]}")
    return tuple(roles)


def create(
    config: LabConfig,
    checkout: Checkout,
    roles: tuple[str, ...],
    account_data: Path | None = None,
    areas: tuple[str, ...] = (),
    deep: bool = False,
    devices: tuple[str, ...] = (),
    purpose: str = "",
) -> Run:
    """Create the run directory and record the sha so gc keeps its worktree."""
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"
    run_dir = config.runs_dir / run_id
    run = Run(run_id, run_dir, roles, account_data, areas, deep, devices, purpose)
    run.captures_dir.mkdir(parents=True)
    meta = {
        "sha": checkout.sha,
        "ref": checkout.ref,
        "purpose": purpose,
        "areas": list(areas),
        "deep": deep,
        "devices": list(devices),
    }
    (run.dir / RUN_META).write_text(json.dumps(meta) + "\n")
    if account_data is None:
        run.data_dir.mkdir()
        run.music_extra_dir.mkdir()
    for name in RUN_SRC:
        source, target = REPO_ROOT / name, run.src_dir / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return run


def record_hold(run: Run, lease_id: str) -> None:
    """Note in run.json the hold that keeps a kept run's lease until `lab release`."""
    meta = run.dir / RUN_META
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "hold": lease_id}) + "\n")


def hold_id(run: Run) -> str | None:
    """Return the hold recorded for a kept run, if any."""
    hold: str | None = json.loads((run.dir / RUN_META).read_text()).get("hold")
    return hold


def load(config: LabConfig, run_id: str) -> Run:
    """Return an existing run: roles and data dir from its compose file, the rest from run.json."""
    run_dir = config.runs_dir / run_id
    services = yaml.safe_load((run_dir / "compose.yml").read_text())["services"]
    roles = tuple(role for role in services["tests"]["environment"]["LAB_ROLES"].split(",") if role)
    [data] = [v.removesuffix(":/data") for v in services["ma"]["volumes"] if v.endswith(":/data")]
    account_data = None if Path(data) == run_dir / "data" else Path(data)
    # run.json of runs created before gate areas, devices or purposes existed lacks those keys
    meta = json.loads((run_dir / RUN_META).read_text())
    areas = tuple(meta.get("areas", ()))
    devices = tuple(meta.get("devices", ()))
    deep = meta.get("deep", False)
    return Run(run_id, run_dir, roles, account_data, areas, deep, devices, meta.get("purpose", ""))


def role_kind(role: str) -> str:
    """Return the player kind of a role, `slim` for `slim-2`."""
    return role.rsplit("-", 1)[0]


def build_ma_image(base: str) -> tuple[str, str]:
    """Build the lab server image on top of `base`; return its tag and the base's repo digest."""
    # --pull moves a tag-only base such as server:stable to its latest release; otherwise
    # docker's layer cache makes this a no-op when neither base nor entrypoint changed
    repo, _, name = base.split("@")[0].rpartition(":")
    tag = f"lab/ma:{name}"
    with tempfile.TemporaryDirectory() as tmp:
        metadata = Path(tmp) / "metadata.json"
        build = ("build", "-q", "--pull", "--build-arg", f"BASE={base}", "-t", tag)
        # min provenance lists the base image digest the build resolved
        _docker(
            *build,
            "--metadata-file",
            str(metadata),
            str(REPO_ROOT / "images/ma"),
            extra_env={"BUILDX_METADATA_PROVENANCE": "min"},
        )
        provenance = json.loads(metadata.read_text())["buildx.build.provenance"]
    # the pull does not move the local base tag, so `docker image inspect <base>` can be stale
    base_uri = f"pkg:docker/{repo}@"
    for material in provenance["materials"]:
        if material["uri"].startswith(base_uri):
            return tag, f"{repo}@sha256:{material['digest']['sha256']}"
    msg = f"no {base} in the build provenance of {tag}"
    raise RuntimeError(msg)


def prepare_worktree(worktree: Path, cliairplay: Path) -> None:
    """Add the gitignored files a source run expects: `.venv` (dev providers) and cliairplay."""
    # nested bind mounts under the read-only /src cannot create their mount points, so these
    # live in the worktree itself
    (worktree / ".venv").mkdir(exist_ok=True)
    target = worktree / CLIAIRPLAY_DIR / cliairplay.name
    if not target.exists():
        # concurrent runs of one sha share the worktree: never expose a half-written binary
        tmp = target.with_name(f".{target.name}.{os.getpid()}")
        shutil.copy2(cliairplay, tmp)
        tmp.replace(target)


def render_compose(
    run: Run,
    config: LabConfig,
    worktree: Path,
    ma_image: str,
    token: str,
    selection: Selection,
) -> dict[str, Any]:
    """Return the compose project for a run as a plain dict."""
    # loopback only: bootstrap on the VM reaches MA, nothing outside the VM does
    ma = ma_service(config, ma_image, worktree, run.data_dir, f"127.0.0.1::{MA_PORT}")
    if run.hardware:
        # macvlan has no port publishing: the VM reaches the server at its LAN address
        del ma["ports"]
    if run.account_data is None:
        # /music is an anonymous volume so it is a mount the server accepts as a music folder
        ma["volumes"] += [
            MUSIC_PATH,
            f"{config.music_dir}:{TONES_MOUNT}:ro",
            f"{run.music_extra_dir}:{EXTRA_MOUNT}",
        ]
    else:
        ma = with_app_vars(config, ma)
    services: dict[str, Any] = {"ma": ma}
    if run.account_data is not None:
        services[POTOKEN_SERVICE] = potoken_service(config)
    else:
        services[RADIO_SERVICE] = _radio_service(run)
    volumes: dict[str, Any] = {}
    for role in run.roles:
        if role_kind(role) == "sendspin":
            services[role] = _sendspin_service(run, role)
            continue
        fifo_volume = f"{role}-fifo"
        volumes[fifo_volume] = {}
        services[f"{role}-pacer"] = _pacer_service(run, role, fifo_volume)
        player = _player_service(run, role, config)
        services[role] = {
            **player,
            "volumes": [*player.get("volumes", []), f"{fifo_volume}:/fifo"],
            # compose stops dependents first, so the pacer finalizes after the player's last bytes
            "depends_on": {f"{role}-pacer": {"condition": "service_healthy"}},
            "stop_grace_period": f"{STOP_TIMEOUT_S}s",
        }
    tests: dict[str, Any] = {
        "image": PLAYERS_IMAGE,
        # started by `run_tests` once bootstrap finished, never by `up`
        "profiles": ["tests"],
        "command": selection.pytest_command(),
        "working_dir": TESTS_SRC_DIR,
        "environment": {
            "LAB_RUN_ID": run.id,
            "LAB_MA_URL": f"http://{service_host(run, 'ma')}:{MA_PORT}",
            "LAB_MA_TOKEN": token,
            "LAB_ROLES": ",".join(run.roles),
            "LAB_CAPTURES_DIR": "/run/captures",
            "LAB_TONES_MANIFEST": "/src/tones/manifest.json",
            "LAB_DEVICES": to_env({device: config.devices[device] for device in run.devices}),
        },
        # scenarios from the lab repo at run creation, so their edits need no image rebuild
        "volumes": [
            f"{run.src_dir}:{TESTS_SRC_DIR}:ro",
            f"{run.dir}:/run",
            f"{run.data_dir}:{TESTS_DATA_DIR}:ro",
        ],
    }
    if run.account_data is None:
        radio_host = service_host(run, RADIO_SERVICE)
        tests["environment"]["LAB_RADIO_URL"] = f"http://{radio_host}:{RADIO_PORT}"
        tests["depends_on"] = {RADIO_SERVICE: {"condition": "service_healthy"}}
        # scenarios stop and restart the server and player containers through the docker
        # engine API
        tests["volumes"].append(f"{DOCKER_SOCKET}:{DOCKER_SOCKET}")
        tests["environment"]["LAB_COMPOSE_PROJECT"] = run.project
    else:
        # the Soloist engine's WebSocket listens on the server's loopback and publishes its
        # port in the data dir, so music scenarios share the server's network namespace
        tests["network_mode"] = "service:ma"
        tests["environment"]["LAB_ACCOUNT_SET"] = run.account_data.parent.name
    services["tests"] = tests
    if run.hardware:
        # several runs share the hardware network, so plain service names would resolve to
        # another run's containers; every service also answers to its project-scoped name
        for name, service in services.items():
            service["networks"] = {"default": {"aliases": [service_host(run, name)]}}
    project: dict[str, Any] = {
        "name": run.project,
        "services": services,
        "networks": {"default": {"name": run.project}},
    }
    if run.hardware:
        network = hardware.require(config).network
        project["networks"] = {"default": {"name": network, "external": True}}
    if volumes:
        project["volumes"] = volumes
    return project


def service_host(run: Run, service: str) -> str:
    """Return the DNS name a run's containers use for one of its services."""
    return f"{run.project}-{service}" if run.hardware else service


def ma_service(
    config: LabConfig, ma_image: str, worktree: Path, data_dir: Path, port: str
) -> dict[str, Any]:
    """Server service: lab image, checkout at /src, `data_dir` at /data, published on `port`."""
    return {
        "image": ma_image,
        # only the uv cache: the server runs the ref's code as root and must not reach the
        # cliairplay binaries other runs copy and execute
        "volumes": [
            f"{worktree}:/src:ro",
            f"{data_dir}:/data",
            f"{config.cache_dir / 'uv'}:/cache/uv",
        ],
        "ports": [port],
    }


def potoken_service(config: LabConfig) -> dict[str, Any]:
    """PO token server the YT Music provider needs; the golden stores it as `http://potoken:4416`."""
    return {"image": config.images["potoken"]}


def with_app_vars(config: LabConfig, service: dict[str, Any]) -> dict[str, Any]:
    """Mount the app vars file read-only into a server service and point the server at it."""
    app_vars = config.app_vars_file
    # docker would create a missing bind-mount source as an empty root-owned directory
    if app_vars is None or not app_vars.is_file():
        msg = f"app vars file {app_vars} not found, set [secrets] app_vars_file in lab.toml"
        raise RuntimeError(msg)
    return {
        **service,
        "volumes": [*service["volumes"], f"{app_vars}:{APP_VARS_PATH}:ro"],
        "environment": {**service.get("environment", {}), "MASS_APP_VARS_FILE": APP_VARS_PATH},
    }


def write_compose(run: Run, project: dict[str, Any]) -> None:
    """Write the compose file of a run."""
    run.compose_file.write_text(yaml.safe_dump(project, sort_keys=False))


def up(run: Run) -> None:
    """Start the server and the player roles."""
    _compose(run, "up", "-d", "--quiet-pull")


def ma_url(run: Run, config: LabConfig) -> str:
    """Return the URL the VM reaches the run's server on: loopback, or its hardware address."""
    if not run.hardware:
        return f"http://{_compose(run, 'port', 'ma', str(MA_PORT)).strip()}"
    container = _compose(run, "ps", "-q", "ma").strip()
    # index, because the network name has a hyphen
    network = hardware.require(config).network
    template = f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}'
    address = _docker("inspect", "-f", template, container).strip()
    return f"http://{address}:{MA_PORT}"


def is_up(run: Run) -> bool:
    """Return whether the run's compose project still has containers."""
    return bool(_compose(run, "ps", "-a", "-q").strip())


def run_tests(run: Run) -> int:
    """Run the tests container in the foreground and return pytest's exit code, 124 on timeout."""
    cmd = ["docker", "compose", "-p", run.project, "-f", str(run.compose_file)]
    try:
        return subprocess.run(
            [*cmd, "run", "--rm", "-T", "tests"], check=False, timeout=TESTS_TIMEOUT_S
        ).returncode
    except subprocess.TimeoutExpired:
        print(f"run {run.id}: tests still running after {TESTS_TIMEOUT_S} s, killed")
        return 124


def teardown(run: Run) -> None:
    """Stop everything, keep the server and player logs, remove the project and hand files back."""
    # SIGTERM lets the capture sidecars finalize their files; stop waits for them to exit
    _compose(run, "stop", "-t", str(STOP_TIMEOUT_S))
    (run.dir / "ma.log").write_text(_compose(run, "logs", "--no-color", "--no-log-prefix", "ma"))
    if run.roles:
        # exit status first: a crashed player logs nothing about its death
        pacers = [f"{role}-pacer" for role in run.roles if role_kind(role) != "sendspin"]
        status = _compose(run, "ps", "-a", "--format", "{{.Name}} {{.Status}}", *run.roles, *pacers)
        logs = _compose(run, "logs", "--no-color", "-t", *run.roles, *pacers)
        (run.dir / "players.log").write_text(status + logs)
    _compose(run, "down", "-v", "--remove-orphans")
    chown_to_me(run.dir)
    if run.account_data is not None:
        chown_to_me(run.account_data)


def chown_to_me(path: Path) -> None:
    """Give files written by root inside containers back to the invoking user."""
    _docker(
        "run",
        "--rm",
        "-v",
        f"{path}:/x",
        PLAYERS_IMAGE,
        "chown",
        "-R",
        f"{os.getuid()}:{os.getgid()}",
        "/x",
    )


def junit_totals(path: Path) -> dict[str, int]:
    """Sum tests, failures, errors and skipped over every testsuite of a JUnit file."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    keys = ("tests", "failures", "errors", "skipped")
    return {key: sum(int(suite.get(key, 0)) for suite in suites) for key in keys}


def testcases(junit: Path) -> dict[str, ET.Element]:
    """Return the testcases of a JUnit file by pytest node id."""
    cases: dict[str, ET.Element] = {}
    for case in ET.parse(junit).iter("testcase"):
        node_id = _node_id(case)
        # a test that fails and then errors in teardown is reported twice; keep the failure
        if node_id not in cases or outcome(case) == "fail":
            cases[node_id] = case
    return cases


def outcome(testcase: ET.Element) -> str:
    """Return `fail` (failure or error), `skip` or `pass` for a JUnit testcase."""
    if testcase.find("failure") is not None or testcase.find("error") is not None:
        return "fail"
    return "skip" if testcase.find("skipped") is not None else "pass"


def failed_node_ids(junit: Path) -> tuple[str, ...]:
    """Return the node ids of the tests that failed or errored in a JUnit file."""
    return tuple(node_id for node_id, case in testcases(junit).items() if outcome(case) == "fail")


def properties(testcase: ET.Element) -> dict[str, str]:
    """Return a JUnit testcase's properties by name; a repeated name keeps its last value."""
    return {prop.get("name", ""): prop.get("value", "") for prop in testcase.iter("property")}


def checkpoint(testcase: ET.Element) -> str:
    """Return the checkpoint a testcase failed at, else its last one, `-` without one."""
    props = properties(testcase)
    return props.get("failed_checkpoint", props.get("checkpoint", "-"))


def failed_checkpoints(junit: Path) -> dict[str, str]:
    """Return the checkpoint of every failed test in a JUnit file by node id."""
    return {
        node_id: checkpoint(case)
        for node_id, case in testcases(junit).items()
        if outcome(case) == "fail"
    }


def rerun_outcomes(failed: tuple[str, ...], failed_again: tuple[str, ...]) -> list[dict[str, str]]:
    """Return the `reruns` entries of a gate run whose `failed` tests were rerun."""
    return [
        {"test": test, "first": "fail", "second": "fail" if test in failed_again else "pass"}
        for test in failed
    ]


def record_rerun(run: Run, rerun_id: str, reruns: list[dict[str, str]]) -> None:
    """Add the rerun's id and per-test outcomes to a written summary.json."""
    path = run.dir / "summary.json"
    summary = {**json.loads(path.read_text()), "rerun_run_id": rerun_id, "reruns": reruns}
    path.write_text(json.dumps(summary, indent=2) + "\n")


def write_summary(
    run: Run, checkout: Checkout, digest: str, duration_s: float, exit_code: int | None
) -> dict[str, Any]:
    """Write and return summary.json; `exit_code` is None when the run failed before pytest."""
    junit = run.dir / "junit.xml"
    summary = {
        "run_id": run.id,
        "ref": checkout.ref,
        "sha": checkout.sha,
        "purpose": run.purpose,
        "image": digest,
        "roles": list(run.roles),
        "areas": list(run.areas),
        "deep": run.deep,
        "duration_s": round(duration_s, 1),
        "exit_code": exit_code,
        "junit": junit_totals(junit) if junit.exists() else None,
    }
    (run.dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def prune_captures(run: Run) -> None:
    """Delete the captures of roles no failed test ran on or used, from a finished run's junit."""
    kept: set[str] = set()
    for node_id, case in testcases(run.dir / "junit.xml").items():
        if outcome(case) != "fail":
            continue
        if node_id.endswith("]"):
            kept.add(node_id.rsplit("[", 1)[1][:-1])
        props = case.iter("property")
        kept.update(prop.get("value", "") for prop in props if prop.get("name") == "role")
    for path in run.captures_dir.iterdir():
        if path.stem not in kept:
            path.unlink()


def _sendspin_service(run: Run, role: str) -> dict[str, Any]:
    """Headless Sendspin client that writes its own capture."""
    return {
        "image": PLAYERS_IMAGE,
        "command": [
            "lab-sendspin",
            "--role",
            role,
            "--dir",
            "/captures",
            "--server",
            service_host(run, "ma"),
            "--identity",
            f"{run.identity_scope}-{role}",
        ],
        "volumes": [f"{run.captures_dir}:/captures"],
        "stop_grace_period": f"{STOP_TIMEOUT_S}s",
    }


def _pacer_service(run: Run, role: str, fifo_volume: str) -> dict[str, Any]:
    """Capture sidecar that creates the role's FIFO and stamps what the player writes into it."""
    command = ["lab-pacer", "--role", role, "--dir", "/captures", "--input", FIFO]
    if role_kind(role) in UNPACED_KINDS:
        command += ["--bytes-per-second", str(PCM_BYTES_PER_SECOND)]
    return {
        "image": PLAYERS_IMAGE,
        "command": command,
        "volumes": [f"{run.captures_dir}:/captures", f"{fifo_volume}:/fifo"],
        # healthy once the FIFO exists, so no player creates a regular file in its place
        "healthcheck": {
            "test": ["CMD", "test", "-p", FIFO],
            "interval": "1s",
            "start_period": "30s",
            "start_interval": "200ms",
        },
        "stop_grace_period": f"{STOP_TIMEOUT_S}s",
    }


def _radio_service(run: Run) -> dict[str, Any]:
    """Mock internet radio the streams scenarios play ICY and HLS from."""
    health = (
        f"import urllib.request; urllib.request.urlopen('http://localhost:{RADIO_PORT}/health')"
    )
    return {
        "image": PLAYERS_IMAGE,
        # from the run's copy, which also carries the tone synthesis the image does not install
        "command": [
            "python3",
            "-m",
            "labctl.radio",
            "--manifest",
            f"{TESTS_SRC_DIR}/tones/manifest.json",
            "--port",
            str(RADIO_PORT),
            "--work",
            "/tmp/radio",
        ],
        "working_dir": TESTS_SRC_DIR,
        "volumes": [f"{run.src_dir}:{TESTS_SRC_DIR}:ro"],
        "healthcheck": {
            "test": ["CMD", "python3", "-c", health],
            "interval": "1s",
            "start_period": "60s",
            "start_interval": "500ms",
        },
        "stop_grace_period": f"{STOP_TIMEOUT_S}s",
    }


def _player_service(run: Run, role: str, config: LabConfig) -> dict[str, Any]:
    """Image and command of a FIFO-writing player role."""
    name = f"lab-{role}"
    match role_kind(role):
        case "airplay":
            # format and FIFO come from the mounted config; the image entrypoint starts avahi
            # and nqptp
            conf = run.src_dir / "images/shairport/shairport-sync.conf"
            return {
                "image": config.images["shairport"],
                "command": ["-a", name],
                "volumes": [f"{conf}:/etc/shairport-sync.conf:ro"],
            }
        case "dlna":
            pipeline = (
                "audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2"
                f" ! filesink location={FIFO} sync=true"
            )
            device_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"ma-lab:{run.identity_scope}:{role}")
            return {
                "image": PLAYERS_IMAGE,
                "command": [
                    "gmediarender",
                    "-f",
                    name,
                    "-u",
                    str(device_uuid),
                    "--logfile",
                    "stdout",
                    "--gstout-audiopipe",
                    pipeline,
                ],
            }
        case "slim":
            squeezelite = (
                # -u X: resample to the output rate even when it is no multiple of the source rate
                f"exec squeezelite -o - -a 16 -r 48000 -R -u X -s ma -m {_mac(run, role)} -n {name}"
            )
            return {"image": PLAYERS_IMAGE, "command": ["sh", "-c", f"{squeezelite} > {FIFO}"]}
        case "snap":
            # MA names a Snapcast client after its host name
            return {
                "image": PLAYERS_IMAGE,
                "hostname": name,
                "command": [
                    "snapclient",
                    "--player",
                    f"file:filename={FIFO}",
                    "--hostID",
                    name,
                    "-h",
                    "ma",
                ],
            }
        case "mpd":
            return {
                "image": PLAYERS_IMAGE,
                "command": ["mpd", "--no-daemon", "--stderr", "/etc/mpd.conf"],
                "volumes": [f"{run.src_dir / 'images/players/mpd.conf'}:/etc/mpd.conf:ro"],
            }
    msg = f"no player service for role {role!r}"
    raise ValueError(msg)


def _node_id(testcase: ET.Element) -> str:
    """Node id in the tests container of a JUnit testcase, from its dotted module classname."""
    module = testcase.get("classname", "").replace(".", "/")
    return f"{TESTS_SRC_DIR}/{module}.py::{testcase.get('name')}"


def _mac(run: Run, role: str) -> str:
    """Locally administered MAC unique to the run's identity scope and role."""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"ma-lab:{run.identity_scope}:{role}").bytes
    return ":".join(f"{b:02x}" for b in (0x02, *digest[:5]))


def _compose(run: Run, *args: str) -> str:
    """Run a docker compose command against the run's project and return stdout."""
    return _docker("compose", "-p", run.project, "-f", str(run.compose_file), *args)


def _docker(*args: str, extra_env: dict[str, str] | None = None) -> str:
    """Run docker and return stdout; stderr is part of the error when it fails."""
    env = {**os.environ, **(extra_env or {})}
    result = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        msg = f"docker {' '.join(args[:3])} failed: {result.stderr.strip()}"
        raise RuntimeError(msg)
    return result.stdout
