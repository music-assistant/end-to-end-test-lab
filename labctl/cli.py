"""labctl: the lab command line, talking to labd and preparing server checkouts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Coroutine, Iterator
from typing import Any

import click

from labctl import accounts, areas, bootstrap, flakes, hardware, refs, runs, seed
from labctl.analyze import Manifest
from labctl.broker_client import BrokerClient, BrokerError, HeldLease
from labctl.config import REPO_ROOT, LabConfig, config_path, load_config
from labctl.leases import account_resource, device_resource
from labctl.plan import RunSpec, plan
from labctl.summary import format_run, format_runs

# pytest's exit code when it collected nothing
NO_TESTS_COLLECTED = 5


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Music Assistant end-to-end test lab."""
    ctx.obj = load_config()


@cli.command()
@click.option("--ref", required=True, help="dev, stable, a branch, a sha or pr/<n>.")
@click.option(
    "--providers",
    "provider_csv",
    help="Comma-separated player and music provider domains, bench device ids, or `devices`.",
)
@click.option("--areas", "area_csv", help="Comma-separated gate areas; core always runs.")
@click.option("--all", "all_areas", is_flag=True, help="Gate over every area and player journey.")
@click.option("--deep", is_flag=True, help="Daily deep run: virtual players, then account sets.")
@click.option("-k", "test_expr", help="pytest -k expression.")
@click.option("--keep", is_flag=True, help="Leave the run up; `lab release <run id>` ends it.")
@click.pass_obj
def run(
    config: LabConfig,
    ref: str,
    provider_csv: str | None,
    area_csv: str | None,
    all_areas: bool,
    deep: bool,
    test_expr: str | None,
    keep: bool,
) -> None:
    """Run scenarios on a ref: a gate, a smoke, music, bench or the deep run, see the runbook."""
    selected = _parse_providers(config, provider_csv) if provider_csv else areas.Providers()
    gate_areas = _parse_areas(area_csv) if area_csv is not None else ()
    try:
        specs = plan(ref, selected, gate_areas, all_areas, deep, keep, test_expr, config.accounts)
    except ValueError as err:
        raise click.UsageError(str(err)) from err
    if selected.devices:
        _check_hardware(config)
    try:
        exit_code = _run_all(config, ref, specs, test_expr, keep)
    except subprocess.CalledProcessError as err:
        raise click.ClickException(f"{' '.join(err.cmd)} failed: {err.stderr or ''}") from err
    except (RuntimeError, OSError, BrokerError) as err:
        raise click.ClickException(str(err)) from err
    sys.exit(exit_code)


@cli.command("runs")
@click.option("--ref", help="Only runs of this ref.")
@click.option("--days", type=int, default=7, show_default=True, help="Look back this many days.")
@click.pass_obj
def list_runs(config: LabConfig, ref: str | None, days: int) -> None:
    """List runs newest first: ref, sha, purpose, exit code, duration and rerun."""
    click.echo(format_runs(config.runs_dir, ref, days))


@cli.command("summary")
@click.argument("run_ids", nargs=-1, required=True)
@click.pass_obj
def run_summary(config: LabConfig, run_ids: tuple[str, ...]) -> None:
    """Print the outcome of runs: failures per area with their checkpoint, and reruns."""
    for run_id in run_ids:
        run_dir = config.runs_dir / run_id
        try:
            data = json.loads((run_dir / "summary.json").read_text())
        except FileNotFoundError as err:
            raise click.ClickException(f"no summary.json in {run_dir}") from err
        click.echo(format_run(data, run_dir / "junit.xml"))


@cli.command("flakes")
@click.option("--days", type=int, default=30, show_default=True, help="Look back this many days.")
@click.option("--list", "as_list", is_flag=True, help="Print every rerun with its checkpoints.")
@click.pass_obj
def flakes_report(config: LabConfig, days: int, as_list: bool) -> None:
    """Print gate reruns per check and how many passed on the second run."""
    if as_list:
        click.echo(flakes.listing(config.flakes_file, days))
    else:
        click.echo(flakes.report(config.flakes_file, days))


@cli.command()
@click.pass_obj
def status(config: LabConfig) -> None:
    """Show granted leases and the queue."""
    state = _run(_client(config).status())
    now = time.time()
    click.echo(f"capacity: {len(state['leases'])}/{state['capacity']} in use")
    for lease in state["leases"]:
        click.echo(f"  {_describe(lease)}  expires in {lease['expires'] - now:.0f}s")
    for position, lease in enumerate(state["queue"], start=1):
        click.echo(f"  queued #{position}  {_describe(lease)}")


@cli.command()
@click.argument("run_id")
@click.pass_obj
def release(config: LabConfig, run_id: str) -> None:
    """Tear down a run kept with --keep and release its hold."""
    if not (config.runs_dir / run_id / "compose.yml").exists():
        raise click.ClickException(f"no run {run_id}")
    kept = runs.load(config, run_id)
    hold = runs.hold_id(kept)
    if runs.is_up(kept):
        runs.teardown(kept)
        click.echo(f"tore down run {kept.id}")
    elif hold is None:
        raise click.ClickException(f"run {kept.id} is not up")
    if hold is not None:
        # the hold may have expired already
        with contextlib.suppress(BrokerError):
            asyncio.run(_client(config).release(hold))
        click.echo(f"released hold {hold}")


@cli.command()
@click.argument("run_id")
@click.pass_obj
def logs(config: LabConfig, run_id: str) -> None:
    """Print the Music Assistant log of a run."""
    path = config.runs_dir / run_id / "ma.log"
    try:
        click.echo(path.read_text(), nl=False)
    except FileNotFoundError as err:
        raise click.ClickException(f"no log at {path}") from err


# the lab-gc timer runs it, not people
@cli.command(hidden=True)
@click.option("--days", type=int, required=True, help="Delete runs older than this.")
@click.pass_obj
def gc(config: LabConfig, days: int) -> None:
    """Delete old runs and the worktrees no remaining run uses."""
    cutoff = time.time() - days * 86400
    run_dirs = list(config.runs_dir.iterdir()) if config.runs_dir.exists() else []
    for run_dir in run_dirs:
        if run_dir.stat().st_mtime < cutoff:
            # a kept or failed run still has root-owned files from its containers
            runs.chown_to_me(run_dir)
            shutil.rmtree(run_dir)
            click.echo(f"removed run {run_dir.name}")
    if not config.refs_dir.exists():
        return
    # under the refs lock, a checkout's run.json exists before its worktree is visible here
    with refs.locked(config):
        # login instances of account sets mount a worktree as well
        metas = [
            *config.runs_dir.glob(f"*/{runs.RUN_META}"),
            *config.accounts_dir.glob(f"*/{runs.RUN_META}"),
        ]
        keep = {json.loads(meta.read_text())["sha"] for meta in metas}
        removed = refs.remove_worktrees(config.mirror, config.refs_dir, keep)
    for path in removed:
        click.echo(f"removed worktree {path.name}")


@cli.group("accounts")
def accounts_group() -> None:
    """Account sets for music runs: log in once on a login instance, then freeze the data dir."""


@accounts_group.command("init")
@click.argument("name")
@click.option("--ref", default="dev", show_default=True, help="Server ref the login instance runs.")
@click.option(
    "--host-network",
    is_flag=True,
    help="Share the VM's network so a phone on VLAN 50 can discover it (Soloist pairing).",
)
@click.pass_obj
def accounts_init(config: LabConfig, name: str, ref: str, host_network: bool) -> None:
    """Start a login instance for a new account set, reachable from the house LAN."""
    with _cli_errors():
        login = accounts.init(config, name, ref, host_network)
    _print_login(name, ref, login)


@accounts_group.command("reopen")
@click.argument("name")
@click.option("--ref", default="dev", show_default=True, help="Server ref the login instance runs.")
@click.option(
    "--host-network",
    is_flag=True,
    help="Share the VM's network so a phone on VLAN 50 can discover it (Soloist pairing).",
)
@click.pass_obj
def accounts_reopen(config: LabConfig, name: str, ref: str, host_network: bool) -> None:
    """Start a login instance on an existing set's golden to redo a provider login."""
    with _cli_errors():
        login = accounts.reopen(
            config, name, ref, lambda: _account_leased(config, name), host_network
        )
    _print_login(name, ref, login)


@accounts_group.command("status")
@click.pass_obj
def accounts_status(config: LabConfig) -> None:
    """List account sets, their login instances and which ones are frozen."""
    with _cli_errors():
        sets = accounts.statuses(config)
    for account_set in sets:
        login = f"login up at {account_set.login_url}" if account_set.login_url else "no login"
        frozen = "frozen" if account_set.frozen else "not frozen"
        click.echo(f"{account_set.name}  {frozen}  {login}")


@accounts_group.command("freeze")
@click.argument("name")
@click.pass_obj
def accounts_freeze(config: LabConfig, name: str) -> None:
    """Stop a set's login instance, mint its token and make it leasable."""
    with _cli_errors():
        accounts.freeze(config, name, config_path())
        _restart_labd()
    click.echo(f"froze {name}: {config.accounts_dir / name}, labd restarted")


@accounts_group.command("remove")
@click.argument("name")
@click.pass_obj
def accounts_remove(config: LabConfig, name: str) -> None:
    """Delete an account set: its login instance, its directory and its lab.toml line."""
    if _account_leased(config, name):
        raise click.ClickException(f"account:{name} is leased or queued, try again after the run")
    with _cli_errors():
        accounts.remove(config, name, config_path())
        if name in config.accounts:
            _restart_labd()
    click.echo(f"removed {name}")


@cli.group("hw")
def hw_group() -> None:
    """Real devices: the macvlan network and host shim device runs join, and the registry."""


@hw_group.command("init")
@click.pass_obj
def hw_init(config: LabConfig) -> None:
    """Install the host shim and create the macvlan network from [hardware]; safe to repeat."""
    with _cli_errors():
        changed = hardware.install(config)
    for line in changed or ["shim and network already in place"]:
        click.echo(line)


@hw_group.command("status")
@click.pass_obj
def hw_status(config: LabConfig) -> None:
    """Check the shim and the macvlan network and list the device registry."""
    with _cli_errors():
        failed = hardware.check(config)
    for line in failed or ["shim and network ready"]:
        click.echo(line)
    for device_id, device in sorted(config.devices.items()):
        click.echo(f"  {device_id}  {device.provider}  {device.name}")
    if failed:
        sys.exit(1)


def main() -> None:
    """Entry point for the `labctl` script."""
    cli()


def _run_all(
    config: LabConfig,
    ref: str,
    specs: tuple[RunSpec, ...],
    test_expr: str | None,
    keep: bool,
) -> int:
    """Lease and run each planned run in order; return the worst exit code."""
    worst = 0
    for spec in specs:
        exit_code = _one_run(config, ref, spec, test_expr, keep)
        # a set whose providers no music scenario covers collects nothing
        if spec.deep and spec.account_set is not None and exit_code == NO_TESTS_COLLECTED:
            exit_code = 0
        worst = max(worst, exit_code)
    return worst


def _one_run(config: LabConfig, ref: str, spec: RunSpec, test_expr: str | None, keep: bool) -> int:
    """Lease and run one planned run, with one rerun of its failures for a gate."""
    names = [account_resource(spec.account_set)] if spec.account_set else []
    names += [device_resource(device) for device in spec.devices]
    # synchronous, so Ctrl-C lands in _test and tears the run down before the release
    with _client(config).lease_sync(spec.purpose, ref, names, on_queued=_print_position) as lease:
        selection = runs.Selection(spec.marker, test_expr)
        run, checkout, exit_code = _execute(config, lease, ref, spec, selection, keep)
        # a kept run stays up as it failed, for debugging
        if spec.rerun and exit_code == 1 and not keep:
            if (run.dir / "junit.xml").exists():
                exit_code = _rerun(config, lease, run, checkout, spec.marker)
            else:
                click.echo(f"run {run.id}: no junit.xml, pytest did not finish, not rerunning")
    return exit_code


def _execute(
    config: LabConfig,
    lease: HeldLease,
    ref: str,
    spec: RunSpec,
    selection: runs.Selection,
    keep: bool,
) -> tuple[runs.Run, refs.Checkout, int]:
    """Check out `ref`, create a run and test it; return the run, checkout and pytest's exit."""
    started = time.monotonic()
    account_set = spec.account_set
    if account_set is not None and accounts.is_open(config, account_set):
        msg = f"account set {account_set} has a login instance, freeze it first"
        raise RuntimeError(msg)
    account_data = config.accounts[account_set] if account_set else None
    with refs.checkout(config, ref) as checkout:
        run = runs.create(
            config,
            checkout,
            spec.roles,
            account_data,
            spec.areas,
            spec.deep,
            spec.devices,
            spec.purpose,
        )
    exit_code = _test(config, lease, run, checkout, account_set, selection, keep, started)
    return run, checkout, exit_code


def _rerun(
    config: LabConfig, lease: HeldLease, first: runs.Run, checkout: refs.Checkout, marker: str
) -> int:
    """Rerun a gate run's failed tests in a fresh run; record the outcome in the first summary."""
    started = time.monotonic()
    failed = runs.failed_node_ids(first.dir / "junit.xml")
    rerun = runs.create(
        config,
        checkout,
        first.roles,
        areas=first.areas,
        devices=first.devices,
        purpose=first.purpose,
    )
    click.echo(f"run {first.id}: rerunning {len(failed)} failed tests as run {rerun.id}")
    selection = runs.Selection(marker, node_ids=failed)
    exit_code = _test(config, lease, rerun, checkout, None, selection, False, started)
    rerun_junit = rerun.dir / "junit.xml"
    # a rerun whose pytest died counts every test as failed again
    failed_again = runs.failed_node_ids(rerun_junit) if rerun_junit.exists() else failed
    reruns = runs.rerun_outcomes(failed, failed_again)
    runs.record_rerun(first, rerun.id, reruns)
    first_checkpoints = runs.failed_checkpoints(first.dir / "junit.xml")
    second_checkpoints = runs.failed_checkpoints(rerun_junit) if rerun_junit.exists() else {}
    flakes.record(
        config.flakes_file, first.id, checkout.ref, reruns, first_checkpoints, second_checkpoints
    )
    return exit_code


def _test(
    config: LabConfig,
    lease: HeldLease,
    run: runs.Run,
    checkout: refs.Checkout,
    account_set: str | None,
    selection: runs.Selection,
    keep: bool,
    started: float,
) -> int:
    """Prepare, boot, bootstrap and test a created run, then tear it down; return pytest's exit."""
    roles = ",".join(run.roles) or "-"
    on = f", devices {','.join(run.devices)}" if run.devices else ""
    on += f", accounts {account_set}" if account_set else ""
    click.echo(f"run {run.id}: {checkout.ref} at {checkout.sha[:12]}, roles {roles}{on}")
    cliairplay = refs.fetch_cliairplay(checkout.worktree, config.cache_dir)
    runs.prepare_worktree(checkout.worktree, cliairplay)
    ma_image, base_digest = runs.build_ma_image(checkout.image)
    tracks: int | None = None
    if account_set is None:
        golden = seed.ensure_golden(config, checkout.worktree, ma_image)
        token = seed.prepare_data(golden, checkout.worktree, run.data_dir)
        tracks = len(Manifest.load(REPO_ROOT / "tones" / "manifest.json").tracks)
    else:
        seed.refresh_default_providers(run.data_dir, checkout.worktree)
        token = accounts.token(config, account_set)
    project = runs.render_compose(run, config, checkout.worktree, ma_image, token, selection)
    runs.write_compose(run, project)
    exit_code: int | None = None
    try:
        runs.up(run)
        devices = {device: config.devices[device] for device in run.devices}
        url = runs.ma_url(run, config)
        asyncio.run(bootstrap.bootstrap(url, token, run.roles, tracks, devices))
        click.echo(f"run {run.id}: bootstrapped after {time.monotonic() - started:.0f} s")
        exit_code = runs.run_tests(run)
    finally:
        if keep:
            # the lease turns into a hold on exit, so the run keeps its slot until `lab release`
            lease.keep_ttl = config.broker.keep_ttl_s
            runs.record_hold(run, lease.id)
        else:
            runs.teardown(run)
        summary = runs.write_summary(
            run, checkout, base_digest, time.monotonic() - started, exit_code
        )
        if not keep:
            runs.prune_captures(run)
        click.echo(
            f"run {run.id}: exit {exit_code}, junit {summary['junit']}, "
            f"{summary['duration_s']:.0f} s, output in {run.dir}"
        )
    return exit_code


def _parse_areas(area_csv: str) -> tuple[str, ...]:
    """Validate a comma-separated --areas value against the known gate areas."""
    selected = tuple(area.strip() for area in area_csv.split(","))
    if unknown := [area for area in selected if area not in areas.AREA_ROLES]:
        msg = f"unknown areas {', '.join(unknown)}, known: {', '.join(areas.AREA_ROLES)}"
        raise click.BadParameter(msg, param_hint="--areas")
    return selected


def _parse_providers(config: LabConfig, provider_csv: str) -> areas.Providers:
    """Resolve a --providers value against the frozen account sets and the device registry."""
    try:
        return areas.parse_providers(provider_csv, config.accounts, config.devices)
    except ValueError as err:
        raise click.BadParameter(str(err), param_hint="--providers") from err


def _check_hardware(config: LabConfig) -> None:
    """Fail before leasing when the shim or the macvlan network a device run needs is missing."""
    with _cli_errors():
        failed = hardware.check(config)
    if failed:
        raise click.ClickException(f"{'; '.join(failed)}; run `lab hw init`")


def _print_login(name: str, ref: str, login: accounts.LoginInstance) -> None:
    """Print where and as whom to log in to a set's login instance."""
    click.echo(f"login instance for {name}: {ref} at {login.sha[:12]}")
    click.echo(f"url       {login.url}")
    click.echo(f"user      {login.username}")
    click.echo(f"password  {login.password}")
    click.echo(f"log in to the providers there, then `lab accounts freeze {name}`")


def _account_leased(config: LabConfig, name: str) -> bool:
    """Return whether a run holds or waits for the set's lease."""
    state = _run(_client(config).status())
    resource = account_resource(name)
    return any(resource in lease["names"] for lease in [*state["leases"], *state["queue"]])


def _restart_labd() -> None:
    """Restart labd so it rebuilds its lease names from lab.toml; leases survive in state.json."""
    subprocess.run(
        ["sudo", "-n", "systemctl", "restart", "labd"], check=True, capture_output=True, text=True
    )


@contextlib.contextmanager
def _cli_errors() -> Iterator[None]:
    """Turn the expected failures of a lab command into CLI errors."""
    try:
        yield
    except subprocess.CalledProcessError as err:
        raise click.ClickException(f"{' '.join(err.cmd)} failed: {err.stderr or ''}") from err
    except (RuntimeError, OSError, ValueError) as err:
        raise click.ClickException(str(err)) from err


def _client(config: LabConfig) -> BrokerClient:
    """Return a broker client for this lab."""
    return BrokerClient(config.broker.socket, config.broker.heartbeat_s)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a broker coroutine, turning broker errors into CLI errors."""
    try:
        return asyncio.run(coro)
    except BrokerError as err:
        raise click.ClickException(str(err)) from err


def _print_position(position: int) -> None:
    """Report the queue position while blocked."""
    click.echo(f"queued at position {position}", err=True)


def _describe(lease: dict[str, Any]) -> str:
    """Format one lease for status output."""
    names = ",".join(lease["names"]) or "-"
    # the broker's owner field carries the run's purpose, which names the ref
    return f"{lease['id']}  {lease['owner']}  {names}"
