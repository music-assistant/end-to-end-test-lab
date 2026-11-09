"""Compose rendering and run summary totals."""

import dataclasses
from pathlib import Path

import pytest

from labctl import runs
from labctl.config import BrokerConfig, HardwareConfig, LabConfig
from labctl.devices import Device
from labctl.refs import Checkout

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="1" tests="7" time="3.2">
<testcase classname="x" name="a" time="1"/></testsuite></testsuites>
"""
# one failed, one errored at setup, one passed, one skipped
GATE_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="scenarios.gate.test_queue" name="test_queue[slim-1]"><failure/></testcase>
<testcase classname="scenarios.gate.test_queue" name="test_queue[dlna-1]"><error/></testcase>
<testcase classname="scenarios.gate.test_core" name="test_core"/>
<testcase classname="scenarios.gate.test_core" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>
"""
SMOKE = runs.Selection("smoke")
MUSIC = runs.Selection("music")


@pytest.fixture
def config(tmp_path: Path) -> LabConfig:
    """Return a lab rooted in a temp dir."""
    broker = BrokerConfig(tmp_path / "sock", tmp_path / "state.json", 1, 120, 30)
    images = {"shairport": "mikebrady/shairport-sync:latest", "potoken": "pot:latest"}
    return LabConfig(tmp_path, broker, tmp_path / "server.git", "upstream", images)


def render(config: LabConfig, players: str) -> dict[str, dict[str, object]]:
    """Render the compose services of a run with the given player kinds."""
    run = runs.Run("r1", config.runs_dir / "r1", runs.parse_roles(players))
    project = runs.render_compose(
        run, config, Path("/wt"), "lab/ma:nightly", "tok", runs.Selection("smoke", "smoke")
    )
    services: dict[str, dict[str, object]] = project["services"]
    return services


def with_bench(config: LabConfig) -> LabConfig:
    """Add a hardware network and two registry devices to a lab."""
    hw = HardwareConfig(
        "lab-hw", "eth0", "10.50.0.0/24", "10.50.0.1", "10.50.0.32/27", "10.50.0.11"
    )
    devices = {"sonos-1": Device("sonos", "Office"), "wiim-1": Device("wiim", "Desk")}
    return dataclasses.replace(config, hardware=hw, devices=devices)


def test_one_sendspin(config: LabConfig) -> None:
    """One sendspin role gets one capture sidecar next to ma and tests."""
    services = render(config, "sendspin")
    assert list(services) == ["ma", "radio", "sendspin-1", "tests"]
    assert services["sendspin-1"]["command"] == [
        "lab-sendspin",
        "--role",
        "sendspin-1",
        "--dir",
        "/captures",
        "--server",
        "ma",
        "--identity",
        "r1-sendspin-1",
    ]
    assert "/wt:/src:ro" in services["ma"]["volumes"]  # type: ignore[operator]
    env = services["tests"]["environment"]
    assert env["LAB_ROLES"] == "sendspin-1"  # type: ignore[index]
    assert env["LAB_MA_URL"] == "http://ma:8095"  # type: ignore[index]
    assert services["tests"]["profiles"] == ["tests"]
    assert services["tests"]["command"][-4:] == [  # type: ignore[index]
        "-k",
        "smoke",
        "--junitxml",
        "/run/junit.xml",
    ]


def test_two_sendspin(config: LabConfig) -> None:
    """Repeating a kind numbers its roles."""
    services = render(config, "sendspin,sendspin")
    assert list(services) == ["ma", "radio", "sendspin-1", "sendspin-2", "tests"]
    assert services["tests"]["environment"]["LAB_ROLES"] == "sendspin-1,sendspin-2"  # type: ignore[index]


def test_sendspin_visualizer(config: LabConfig) -> None:
    """A visualizer role is a Sendspin client in visualizer mode, without a pacer."""
    services = render(config, "sendspin,sendspinviz")
    assert list(services) == ["ma", "radio", "sendspin-1", "sendspinviz-1", "tests"]
    command = services["sendspinviz-1"]["command"]
    assert command[:3] == ["lab-sendspin", "--role", "sendspinviz-1"]  # type: ignore[index]
    assert command[-1] == "--visualizer"  # type: ignore[index]
    assert "--visualizer" not in services["sendspin-1"]["command"]  # type: ignore[operator]


@pytest.mark.parametrize(
    ("kind", "command_head"),
    [
        ("airplay", ["-a", "lab-airplay-1"]),
        ("dlna", ["gmediarender", "-f", "lab-dlna-1"]),
        ("slim", ["sh", "-c"]),
        ("snap", ["snapclient", "--player", "file:filename=/fifo/pcm", "--hostID", "lab-snap-1"]),
        ("mpd", ["mpd", "--no-daemon"]),
    ],
)
def test_fifo_player(config: LabConfig, kind: str, command_head: list[str]) -> None:
    """A FIFO-writing player starts after its pacer sidecar and shares the FIFO volume with it."""
    role = f"{kind}-1"
    project = runs.render_compose(
        runs.Run("r1", config.runs_dir / "r1", (role,)), config, Path("/wt"), "img", "tok", SMOKE
    )
    services = project["services"]
    assert list(services) == ["ma", "radio", f"{role}-pacer", role, "tests"]
    player, pacer = services[role], services[f"{role}-pacer"]
    assert player["command"][: len(command_head)] == command_head
    assert player["depends_on"] == {f"{role}-pacer": {"condition": "service_healthy"}}
    assert f"{role}-fifo:/fifo" in player["volumes"]
    assert f"{role}-fifo:/fifo" in pacer["volumes"]
    assert project["volumes"] == {f"{role}-fifo": {}}
    # only players that write as fast as the FIFO drains get a rate-limited pacer
    assert ("--bytes-per-second" in pacer["command"]) == (kind in ("slim", "mpd"))


def test_load_roles_from_compose(config: LabConfig) -> None:
    """Pacer sidecars are not roles when a kept run is loaded back."""
    run = runs.Run("r1", config.runs_dir / "r1", runs.parse_roles("slim,sendspin"))
    run.dir.mkdir(parents=True)
    runs.write_compose(run, runs.render_compose(run, config, Path("/wt"), "img", "tok", SMOKE))
    (run.dir / runs.RUN_META).write_text("{}")
    assert runs.load(config, "r1").roles == ("slim-1", "sendspin-1")


def test_music_golden_and_app_vars(config: LabConfig, tmp_path: Path) -> None:
    """A music run mounts the account golden in place and the app vars file read-only."""
    app_vars = tmp_path / "app_vars.json"
    app_vars.write_text("{}")
    config = dataclasses.replace(config, app_vars_file=app_vars)
    golden = tmp_path / "accounts" / "spotify-a" / "data"
    run = runs.Run("r1", config.runs_dir / "r1", ("sendspin-1",), golden)
    project = runs.render_compose(run, config, Path("/wt"), "img", "tok", MUSIC)
    ma, tests = project["services"]["ma"], project["services"]["tests"]
    assert f"{golden}:/data" in ma["volumes"]
    assert f"{app_vars}:/secrets/app_vars.json:ro" in ma["volumes"]
    assert ma["environment"] == {"MASS_APP_VARS_FILE": "/secrets/app_vars.json"}
    assert not any(volume.endswith(":/music:ro") for volume in ma["volumes"])
    assert tests["command"][4:6] == ["-m", "music"]
    assert tests["environment"]["LAB_ACCOUNT_SET"] == "spotify-a"
    assert project["services"]["potoken"] == {"image": "pot:latest"}
    run.dir.mkdir(parents=True)
    runs.write_compose(run, project)
    (run.dir / runs.RUN_META).write_text("{}")
    assert runs.load(config, "r1").account_data == golden


def test_music_tests_share_server_network(config: LabConfig, tmp_path: Path) -> None:
    """Music scenarios reach the server's loopback; every run reads the server's data dir."""
    app_vars = tmp_path / "app_vars.json"
    app_vars.write_text("{}")
    config = dataclasses.replace(config, app_vars_file=app_vars)
    golden = tmp_path / "data"
    music = runs.Run("r1", config.runs_dir / "r1", ("sendspin-1",), golden)
    project = runs.render_compose(music, config, Path("/wt"), "img", "tok", MUSIC)
    tests = project["services"]["tests"]
    assert tests["network_mode"] == "service:ma"
    assert f"{golden}:/data:ro" in tests["volumes"]
    fresh = runs.Run("r2", config.runs_dir / "r2", ("sendspin-1",))
    project = runs.render_compose(fresh, config, Path("/wt"), "img", "tok", SMOKE)
    tests = project["services"]["tests"]
    assert "network_mode" not in tests
    assert f"{fresh.data_dir}:/data:ro" in tests["volumes"]


def test_music_player_identities_per_set(config: LabConfig, tmp_path: Path) -> None:
    """Music run player identities follow the account set, not the run id."""
    app_vars = tmp_path / "app_vars.json"
    app_vars.write_text("{}")
    config = dataclasses.replace(config, app_vars_file=app_vars)
    golden = tmp_path / "accounts" / "spotify-a" / "data"
    roles = ("sendspin-1", "dlna-1", "slim-1")
    first, second = (
        runs.render_compose(
            runs.Run(run_id, config.runs_dir / run_id, roles, golden),
            config,
            Path("/wt"),
            "img",
            "tok",
            MUSIC,
        )["services"]
        for run_id in ("r1", "r2")
    )
    assert first["sendspin-1"]["command"][-1] == "spotify-a-sendspin-1"
    for role in roles:
        assert first[role]["command"] == second[role]["command"]


def test_music_needs_app_vars_file(config: LabConfig, tmp_path: Path) -> None:
    """A missing app vars file fails before docker turns its path into a directory."""
    config = dataclasses.replace(config, app_vars_file=tmp_path / "missing.json")
    run = runs.Run("r1", config.runs_dir / "r1", ("sendspin-1",), tmp_path / "data")
    with pytest.raises(RuntimeError, match="app vars file"):
        runs.render_compose(run, config, Path("/wt"), "img", "tok", MUSIC)


def test_unknown_kind() -> None:
    """Only implemented player kinds are accepted."""
    with pytest.raises(ValueError, match="unknown player kind"):
        runs.parse_roles("sendspin,chromecast")


def test_junit_totals(tmp_path: Path) -> None:
    """Totals come from the testsuite attributes."""
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    assert runs.junit_totals(path) == {"tests": 7, "failures": 2, "errors": 1, "skipped": 1}


@pytest.mark.parametrize(
    ("selection", "args"),
    [
        (runs.Selection("smoke"), ["/src/scenarios", "-p", "no:cacheprovider", "-m", "smoke"]),
        (
            runs.Selection("gate", "smoke"),
            ["/src/scenarios", "-p", "no:cacheprovider", "-m", "gate", "-k", "smoke"],
        ),
        (
            runs.Selection("gate", node_ids=("/src/a.py::t[x]", "/src/b.py::t")),
            ["/src/a.py::t[x]", "/src/b.py::t", "-p", "no:cacheprovider", "-m", "gate"],
        ),
    ],
)
def test_pytest_command(selection: runs.Selection, args: list[str]) -> None:
    """Node ids replace the scenarios dir; -k only follows the marker when given."""
    assert selection.pytest_command() == ["pytest", *args, "--junitxml", "/run/junit.xml"]


def test_run_json_round_trip(config: LabConfig) -> None:
    """Areas, the deep flag and the purpose come back from run.json when a run is loaded."""
    checkout = Checkout("dev", "a" * 40, Path("/wt"), "img")
    run = runs.create(
        config, checkout, ("slim-1",), areas=("queue",), deep=True, purpose="gate dev queue"
    )
    runs.write_compose(run, runs.render_compose(run, config, Path("/wt"), "img", "tok", SMOKE))
    loaded = runs.load(config, run.id)
    assert (loaded.areas, loaded.deep, loaded.purpose) == (("queue",), True, "gate dev queue")


def test_fresh_run_mounts_docker_socket(config: LabConfig, tmp_path: Path) -> None:
    """A run without an account set hands the tests container the docker socket and project."""
    tests = render(config, "slim")["tests"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in tests["volumes"]  # type: ignore[operator]
    assert tests["environment"]["LAB_COMPOSE_PROJECT"] == "lab-r1"  # type: ignore[index]
    app_vars = tmp_path / "app_vars.json"
    app_vars.write_text("{}")
    config = dataclasses.replace(config, app_vars_file=app_vars)
    run = runs.Run("r1", config.runs_dir / "r1", ("slim-1",), tmp_path / "data")
    music = runs.render_compose(run, config, Path("/wt"), "img", "tok", MUSIC)["services"]["tests"]
    assert not any("docker.sock" in volume for volume in music["volumes"])
    assert "LAB_COMPOSE_PROJECT" not in music["environment"]


def test_fresh_run_music_folders(config: LabConfig) -> None:
    """Without an account set the tone library and the run's writable folder are below /music."""
    checkout = Checkout("dev", "a" * 40, Path("/wt"), "img")
    run = runs.create(config, checkout, ("slim-1",))
    assert run.music_extra_dir.is_dir()
    services = runs.render_compose(run, config, Path("/wt"), "img", "tok", SMOKE)["services"]
    assert services["ma"]["volumes"][-3:] == [
        "/music",
        f"{config.music_dir}:/music/tones:ro",
        f"{run.music_extra_dir}:/music/extra",
    ]
    assert f"{run.dir}:/run" in services["tests"]["volumes"]


def test_failed_node_ids(tmp_path: Path) -> None:
    """Failures and errors are failed, as container node ids; passes and skips are not."""
    path = tmp_path / "junit.xml"
    path.write_text(GATE_JUNIT)
    assert runs.failed_node_ids(path) == (
        "/src/scenarios/gate/test_queue.py::test_queue[slim-1]",
        "/src/scenarios/gate/test_queue.py::test_queue[dlna-1]",
    )


def test_failed_checkpoints(tmp_path: Path) -> None:
    """Failed tests map to their failed or else last checkpoint, `-` without one."""
    path = tmp_path / "junit.xml"
    path.write_text(
        GATE_JUNIT.replace(
            "<failure/>",
            '<properties><property name="checkpoint" value="play"/>'
            '<property name="failed_checkpoint" value="seek"/></properties><failure/>',
        )
    )
    assert runs.failed_checkpoints(path) == {
        "/src/scenarios/gate/test_queue.py::test_queue[slim-1]": "seek",
        "/src/scenarios/gate/test_queue.py::test_queue[dlna-1]": "-",
    }


def test_rerun_outcomes() -> None:
    """A rerun test passes on the second try unless it failed again."""
    assert runs.rerun_outcomes(("a", "b"), ("b",)) == [
        {"test": "a", "first": "fail", "second": "pass"},
        {"test": "b", "first": "fail", "second": "fail"},
    ]


def test_fresh_run_radio(config: LabConfig, tmp_path: Path) -> None:
    """A run without an account set has the mock radio; a music run has none."""
    services = render(config, "sendspin")
    assert services["radio"]["image"] == runs.PLAYERS_IMAGE
    assert services["tests"]["environment"]["LAB_RADIO_URL"] == "http://radio:8000"  # type: ignore[index]
    app_vars = tmp_path / "app_vars.json"
    app_vars.write_text("{}")
    config = dataclasses.replace(config, app_vars_file=app_vars)
    run = runs.Run("r1", config.runs_dir / "r1", ("sendspin-1",), tmp_path / "data")
    services = runs.render_compose(run, config, Path("/wt"), "img", "tok", MUSIC)["services"]
    assert "radio" not in services
    assert "LAB_RADIO_URL" not in services["tests"]["environment"]


def test_hardware_profile(config: LabConfig) -> None:
    """A device run joins the shared macvlan network, publishes no port and lists its devices."""
    config = with_bench(config)
    run = runs.Run("r1", config.runs_dir / "r1", ("sendspin-1",), devices=("sonos-1",))
    project = runs.render_compose(run, config, Path("/wt"), "img", "tok", SMOKE)
    assert project["networks"] == {"default": {"name": "lab-hw", "external": True}}
    assert "ports" not in project["services"]["ma"]
    env = project["services"]["tests"]["environment"]
    assert env["LAB_DEVICES"] == '{"sonos-1": {"name": "Office", "provider": "sonos"}}'
    # service names are shared with other runs on that network: containers use scoped aliases
    assert project["services"]["ma"]["networks"] == {"default": {"aliases": ["lab-r1-ma"]}}
    assert env["LAB_MA_URL"] == "http://lab-r1-ma:8095"
    assert env["LAB_RADIO_URL"] == "http://lab-r1-radio:8000"
    assert "lab-r1-ma" in project["services"]["sendspin-1"]["command"]


def test_bridge_profile_has_no_devices(config: LabConfig) -> None:
    """A bridge run publishes the server port and hands the scenarios an empty device map."""
    services = render(config, "sendspin")
    assert services["ma"]["ports"] == ["127.0.0.1::8095"]
    assert services["tests"]["environment"]["LAB_DEVICES"] == "{}"  # type: ignore[index]


def test_load_devices(config: LabConfig) -> None:
    """Leased devices come back from run.json when a run is loaded."""
    config = with_bench(config)
    checkout = Checkout("dev", "a" * 40, Path("/wt"), "img")
    run = runs.create(config, checkout, ("sendspin-1",), devices=("sonos-1",))
    runs.write_compose(run, runs.render_compose(run, config, Path("/wt"), "img", "tok", SMOKE))
    assert runs.load(config, run.id).devices == ("sonos-1",)
