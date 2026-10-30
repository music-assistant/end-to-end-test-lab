# Runbook

## What the lab is

The lab runs a Music Assistant ref against a fresh server and virtual players (Sendspin, AirPlay, DLNA, Squeezelite, Snapcast, MPD) on a private Docker network, captures the audio each player received, and returns pytest's exit code, JUnit XML, the server log and the captures. It lives on a dedicated VM on an isolated network segment, so no lab player can reach a real speaker in the house. A run without a music provider gets a fresh data dir seeded from `golden/tier1` (the tone library); a music run mounts the account set's golden. A small broker on the VM hands out leases so several agents can run at once.

`lab` is `bin/lab` on your machine: it rsyncs the repo to `/srv/lab/src` on the VM and runs `labctl` there over ssh. Put it on your PATH. Set `LAB_SSH_HOST` (default `lab`) or `LAB_SRC` (default `/srv/lab/src`) to point it elsewhere.

```
lab run --ref dev                                             # smoke test, every player kind once
lab run --ref pr/7244 --providers sendspin,snapcast           # one PR, core plus two player journeys
lab run --ref pr/7244 --areas queue,streams                   # PR gate: core plus two areas, one rerun of failures
lab run --ref pr/7244 --providers airplay                     # PR gate: core plus the AirPlay journey
lab run --ref pr/7244 --all                                   # PR gate: every area and journey
lab run --ref dev --providers spotify                         # music run on the Spotify account set
lab run --ref dev --deep                                      # daily deep run: virtual players, then every account set
lab run --ref dev --providers sonos-1 -k play_stop            # real device on the bench, see Hardware
lab runs                                                      # recent runs: purpose, exit code, rerun
lab summary <run id>                                          # failures per area, checkpoint, rerun outcome
lab status                                                    # leases and queue
lab logs <run id>                                             # server log of a run
lab release <run id>                                          # tear down a run kept with --keep, end its hold
lab accounts init spotify-a                                   # see Account sets
lab accounts reopen spotify-a                                 # redo a login on an existing set
```

## Network and VM

The network (an isolated VLAN with a drop rule towards the home LAN, an SSID on it, a trunk to
the hypervisor; UniFi and Proxmox as worked examples), the VM, its bootstrap, the config file, images, mirror, tone library and the account
sets are built step by step in [`SETUP.md`](../SETUP.md) at the repo root, together with the
isolation checks to rerun after any network change and the layout of `/srv/lab`. Rebuilding the
VM is the same procedure from step 3 on.

## Running tests

```
lab run --ref <ref> [--providers <list>] [--areas <areas> | --all | --deep] [-k <expr>] [--keep]
```

- `--ref` is required.
- `--providers` is a comma-separated list. Each entry is one of:
  - a virtual player provider by its Music Assistant domain: `sendspin`, `airplay`, `dlna`, `squeezelite`, `snapcast`, `mpd` (`slim` and `snap` work too). Repeat one for more players of it: `squeezelite,squeezelite` gives `slim-1` and `slim-2`. Each role shows up in Music Assistant as `lab-<role>`. Without any, a run gets one of each.
  - a music provider by its domain: `spotify`, `apple_music`, `tidal`, `ytmusic`, `qobuz`, `deezer`. It resolves to the frozen account set of that provider (`spotify` is `spotify-a`) and fails when there is none or more than one; a set name such as `spotify-a` works as well.
  - a bench device id from `[devices]` (`sonos-1`), or `devices` for every registered one; see Hardware.
- `--areas` is a comma-separated list of gate areas (`AREA_ROLES` in `labctl/areas.py`: `core`, `queue`, `streams`, `players`, `library`).
- `-k` is a pytest `-k` expression. It narrows whichever mode the other flags picked; the smoke test only runs when nothing is listed. The smoke run has the smoke test, parametrized over every role; music runs have `continuity`, `external_pause`, `last_track_ends_clean` and `provider_reload`, see Account sets.
- `--keep` leaves the run up instead of tearing it down and keeps its lease as a hold, see below.

The flags pick what runs:

- `--areas <a>,<b>`: a gate with `-m "gate and (area_core or area_<a> ...)"`, `core` always included. The roles are the union of the areas' roles, or the listed player providers plus `slim-1` for `core`, so `core` never skips.
- Player providers without `--areas`, `--all` or `--deep`: a gate over `core` and each provider's journey, `-m "gate and (area_core or provider_<domain> ...)"`, on the listed players plus `slim-1` for `core`.
- `--all`: a gate over everything, `-m gate`: every area and every journey, on every area's roles plus one player of each kind.
- Music providers: one music run per account set (`-m music`), one after another, each under its own lease, on the first listed player provider or `sendspin-1`. No rerun.
- Devices: one bench run, see Hardware.
- `--deep`: the daily deep run: one run with `-m deep` on every deep role (`DEEP_ROLES`, two of slim, snap and sendspin) or the listed player providers, then one music run per frozen account set in name order, each on `sendspin-1` under its own lease. A music run that collects nothing (5) counts as 0.
- Nothing of the above: a smoke run, `-m smoke`, no rerun.

With a music provider in the list the players are only its stage: `--providers squeezelite,spotify` runs Spotify on `slim-1`, no squeezelite gate. `--areas`, `--all` and `--deep` exclude each other, `--deep` refuses `--keep`, `--deep` and `--all` take no music providers or devices, `--areas` takes no devices, and devices go only with `sendspin` players. Several runs exit with the worst exit code; each prints its own id.

Every run has a purpose, which the lease, `run.json` and `summary.json` carry and `lab status` and `lab runs` show: `gate <ref> <areas> [<domains>]`, `all <ref>`, `deep <ref>` and `deep <ref> <set>` for its music runs, `music <ref> <set>`, `smoke <ref>` or `bench <ref> <ids>`, plus ` keep` with `--keep`. A gate's rerun has the same purpose.

Which areas a PR needs is in [pr-gate.md](pr-gate.md).

Ref forms are `dev`, `stable`, any branch name, a sha, or `pr/<n>` (fetched from `refs/pull/<n>/head`). The mirror is fetched on every run. The base image follows from the sha: a sha on `stable`, or forked from `stable` more recently than from `dev`, runs on the stable image, anything else on the nightly digest in `lab.toml`. The ref's checkout is mounted read-only at `/src`, and the server container installs the ref's `requirements_all.txt` at start through a shared uv cache.

The CLI prints the run id and sha, how long bootstrap took, and a closing line with the exit code, JUnit totals, duration and output directory. The exit code of `lab run` is pytest's: 0 passed, 1 test failures, 5 nothing collected. Each test fails when it overruns its layer's budget (`pytest.mark.timeout` in the directory's `pytestmark`, else 900 s from `pyproject.toml`), and the run goes on with the next test; the `ctx` fixture stops the test's player at teardown, since a test killed by its budget runs its own `finally` only after the client has closed. A tests container still running after 2 h is killed and `lab run` exits 124. If labd dropped the run's lease during the run (it expired or was released), `lab run` still writes `summary.json` and then fails with `lease <id> was lost during the run`.

Ctrl-C tears the run down (unless `--keep`) and then releases the lease.

Gate rerun. When a gate run exits 1 and wrote `junit.xml` (test failures, not 2 to 5, a failure before pytest or a pytest that died), `lab run` reruns the failed and errored tests once, by node id with the same marker, as a new run in a fresh compose project under the same lease and checkout. The command's exit code is the rerun's, so the gate is red only when a test fails twice. The first run's `summary.json` gets `rerun_run_id` and `reruns` (`[{"test": <node id>, "first": "fail", "second": "pass"|"fail"}]`); the rerun's `junit.xml`, captures and `ma.log` live in its own run dir. `--keep` skips the rerun and keeps the failed run up. Smoke, music, bench and `--deep` runs never rerun.

Flake ledger. Every gate rerun appends one line per rerun test to `/srv/lab/flakes.jsonl` (date, run id, ref, node id, role, check, first and second outcome, and `first_checkpoint` and `second_checkpoint`: the failed or else last checkpoint of each run, `-` when that run passed or recorded none). `lab flakes [--days n]` (default 30) prints one line per check, most rerun first: `<check>  reruns <n>  passed on rerun <k>  roles <roles>`. `lab flakes --list` prints the entries themselves, newest first: `<date>  <run id>  <ref>  <node id>  fail (<checkpoint>) -> pass (-)`. Lines written before the checkpoints were recorded show `-`.

```
lab runs [--ref <ref>] [--days <n>]
```

prints one line per run of the last `n` days (default 7), newest first, optionally of one ref: run id, ref, sha, purpose, `exit <code>` (`exit -` while running or when `summary.json` is missing, `exit boot-failed` when the run failed before pytest), duration, and `rerun <id>` for a gate that was rerun.

```
lab summary <run id> [<run id> ...]
```

prints per run either `run <id>: green (<n> tests, <s> s)`, or a header with ref, sha, exit code and duration, one `area <name>: <passed>/<total> passed` line per area (the `area` testcase property, `-` without one; skips counted apart) with a `FAIL` line per failed test (node id, the failed checkpoint or else the last one, issues), and the rerun outcome. A run without an exit code failed before pytest.

A run leaves `/srv/lab/runs/<run id>/` (the id is a UTC timestamp plus a suffix):

```
summary.json      run_id, ref, sha, purpose, base image digest the build used (from its provenance), roles, areas, deep, duration_s, exit_code, junit totals; a rerun gate run adds rerun_run_id and reruns
junit.xml         pytest results
ma.log            server log, written at teardown
players.log       player and pacer container logs with timestamps, written at teardown; a snapclient or squeezelite that died or hung is diagnosed here
captures/         <role>.pcm, .stamps and .json per role: the audio received, per-write timestamps, format
compose.yml       the rendered compose project, named lab-<run id>
run.json          ref, sha (so gc keeps the worktree), purpose, areas, deep and devices; written first, before any slow step. Kept runs add the hold id
src/              copy of what the run mounts from the lab repo (labctl, scenarios, pyproject.toml, tone manifest, mpd and shairport configs)
data/             the server data dir (not in music runs, which use the account set's data dir), read-only at /data in the tests container
music-extra/      not in music runs: writable music folder, /music/extra in the server (the tone library is /music/tones) and /run/music-extra in the tests container
```

Reading a failed run:

1. Look at `summary.json`. `exit_code: null` and `junit: null` mean the run failed before pytest, and the CLI error says where. Bootstrap waits 180 s for the server, 60 s for the tone library to reach 8 tracks and 60 s for all roles to register (`players missing after 60 s: [...]`).
2. `lab summary <run id>` names the failing tests per area with their checkpoint. Open `junit.xml` for the failing test and its message. When one role is missing, only that role's case errors at setup (`player not available after 60 s: <role>`).
3. `lab logs <run id>` prints `ma.log`. Search it for the player's name (`lab-<role>`), provider errors and stream start and stop lines. A role that went unavailable: `players.log` starts with each player container's exit status and holds its log.
4. For audio problems read the capture with the analyzer. Segment kinds are a tone id (`a1t1` and so on), `silence` (below -60 dB), `mixed` and `unknown`. Times are wall-clock epoch seconds, and -12 dB is the tone at full volume.

```
ssh lab 'cd /srv/lab/src && .venv/bin/python' <<'EOF'
from pathlib import Path
from labctl.analyze import Capture, Manifest

manifest = Manifest.load(Path("tones/manifest.json"))
cap = Capture.load(Path("/srv/lab/runs/<run id>/captures"), "airplay-1", manifest)
for segment in cap.timeline():
    print(segment)
print(cap.gaps(), cap.overlaps(), cap.markers())
EOF
```

5. To listen, convert a capture (the format is in `<role>.json`).

```
ssh lab 'ffmpeg -f s16le -ar 48000 -ac 2 -i /srv/lab/runs/<run id>/captures/airplay-1.pcm /tmp/airplay-1.wav'
```

6. A failed testcase in `junit.xml` has `checkpoint` and `issues` properties, and `failed_checkpoint` and `failed_issues` for the step that failed. A checkpoint is `<check>.<step>`, such as `album_plays_through.tones_in_order`, so a journey's failure names the check as well as the step. `started_at` and `ended_at` are the test's wall-clock start and end in epoch seconds, the clock of the capture timelines and of the events below. The failure text of a test that failed in its call phase ends with an `events` section: the test's last 20 server events, one per line with time, event type, object id and the state, index, elapsed time or volume they carry.

`--keep` skips teardown, so the compose project `lab-<run id>` stays up and `ma.log` and `players.log` do not exist yet. The server is published on the VM's loopback only. To poke at it:

```
ssh lab 'docker compose -p lab-<run id> logs ma'
ssh lab 'docker compose -p lab-<run id> port ma 8095'
ssh lab 'cd /srv/lab/runs/<run id> && docker compose -p lab-<run id> -f compose.yml run --rm -T tests'   # rerun pytest
lab release <run id>                                                                                      # tear down, writes ma.log and players.log, ends the hold
```

When `lab run --keep` returns, labd turns the run's lease into a hold of `keep_ttl_s` seconds (`[broker]` in `lab.toml`, default 14400, so 4 h) and `run.json` records its id. The kept run keeps its slot and account set and shows in `lab status` as a lease until `lab release <run id>` tears it down and releases the hold. If the hold expires first, the project stays up but no longer counts against capacity. Rerunning pytest uses the run's own `src/` copy, so later syncs of the repo do not reach it.

Timings, warm (images, worktree, golden and uv cache present), wall clock from the machine running `bin/lab`:

- All six roles: 64 to 68 s.
- One role each: airplay 41 s, dlna 37 s, snap 30 s, mpd 26 s, slim 23 s.
- A cold uv cache adds about 30 s at server start, a warm one 2.5 s.

## Leases

A run takes one capacity slot, plus `account:<set>` for a music run and `device:<id>` per leased device. Capacity is `[broker] capacity` in `lab.toml` (2 in the example), so by default two runs go at once and a third queues. Waiters are served first in, first out, but a waiter blocked on a busy account does not hold up a later waiter whose resources are free. A `lab run` that has to wait prints `queued at position N` on stderr.

```
lab status
lab release <run id>
```

`lab status` prints `capacity: <used>/<total> in use`, one line per granted lease (id, purpose, resources, seconds until it expires) and one line per queued request (`queued #n`). For manual debugging, run with `--keep`: `lab release <run id>` tears the kept run down and releases the hold it kept.

A running `lab run` heartbeats every 30 s (`heartbeat_s`) from a helper thread and its lease lasts 120 s (`lease_ttl_s`). If the client dies, the lease is freed within the TTL. A failed heartbeat is retried at the next interval; if labd no longer knows the lease, the run finishes and `lab run` then fails (see Running tests). Queued requests stay alive by polling in the same way.

labd keeps its state in `/srv/lab/state.json`, rewritten after every change, and loads it on start. After a restart the granted leases and the queue are back, clients retry the socket for 15 s, and a lease that expired while labd was down is dropped by the first sweep. labd reads the `[accounts]` and `[devices]` tables only at start, so adding or removing a set or a device needs `sudo systemctl restart labd`, which `lab accounts freeze` and `lab accounts remove` do themselves. systemd restarts labd after a crash.

## Account sets

An account set is a Music Assistant data dir with streaming providers logged in (for example
`spotify-a`, `apple-a`). Music runs boot it in place, one run per set at a time: every set is an
exclusive lease `account:<set>` next to the run slot. Everything lives in `/srv/lab/accounts/<set>/`:
`data/` (the golden data dir), `password` (UI login), `token` (API token for runs).

The server reads the Spotify, Apple Music and Tidal client ids from the app vars file
(`[secrets] app_vars_file` in `lab.toml`, `/srv/lab/secrets/app_vars.json`, copied from the
`appvars` repo). Login instances and music runs refuse to start without it.

### Log in

```
lab accounts init spotify-a [--ref dev]
```

starts a login instance on a fresh data dir: compose project `lab-acct-spotify-a` on its own
bridge, no players, published on the VM's VLAN address. The command prints the URL
(`http://10.50.0.10:8095`, the next free port from 8095 up when another set is logging in),
user `lab` and the password. Open the URL from the house LAN, add the providers and finish their
sign-ins (Spotify OAuth and Soloist pairing, Apple Music, Tidal, YT Music). The instance is a
detached compose project with `restart: unless-stopped`, so it stays up across shells and VM
reboots until it is frozen. `lab accounts status` lists the sets, the running login instances and
which sets are frozen.

One set per service (`spotify-a`, `apple-a`, `tidal-a`, `ytm-a`, `qobuz-a`, `deezer-a`): a login
that expires is redone for that set alone, a broken provider cannot fail another service's run,
and the sets lease independently. Keep one provider per set.

Spotify with the Soloist backend pairs from the Spotify app on a phone, which must discover the
server over mDNS. A container on a private bridge is invisible to it, so start that set with
`lab accounts init spotify-a --host-network`: the server then shares the VM's network (web and
stream ports picked free on every interface) and the phone joins the `MA Lab` Wi-Fi (VLAN 50).
Autoplay must be OFF on the Spotify account, see Music scenarios.

YT Music needs a PO token server. Login instances (except host-network ones) and music runs start
a `potoken` sidecar (`[images] potoken`, bgutil-ytdlp-pot-provider); enter `http://potoken:4416`
as the PO token server URL in the YT Music setup flow so the golden keeps a URL every run resolves.

### Freeze

```
lab accounts freeze spotify-a
```

mints a 365 day API token into `token`, stops and removes the login instance, deletes the
`webrtc_*.pem` pair (the remote access identity), sets the webserver base URL back to auto, adds
`spotify-a = "/srv/lab/accounts/spotify-a/data"` under `[accounts]` in `lab.toml` and restarts
labd (`sudo systemctl restart labd`) so the lease `account:spotify-a` exists. Leases survive the
restart in `state.json` and clients retry for 15 s, so running jobs are not affected.

### Run

```
lab run --ref dev --providers spotify [-k expr] [--keep]
```

holds a run slot plus `account:spotify-a`, mounts the set's `data/` at `/data` without copying
(whatever the run changes stays in the golden: player providers, players, library; player identities are fixed per set and role, so the players are reused across runs), passes the app
vars file, and runs `pytest -m music`. Bootstrap only adds the player providers of the requested
roles; the account's providers come with the golden. Runs have internet through the bridge NAT
and no route to the house LAN.

In a music run the tests container shares the server's network namespace
(`network_mode: service:ma`) and mounts the set's data dir read-only at `/data`: scenarios read the
live server log (`/data/musicassistant.log`) and reach the Soloist engine's WebSocket, which listens
on the server's loopback and publishes its port in `/data/spotify/<instance>/soloist-data/ws.port`.

### Music scenarios

- `continuity` (every set): two tracks of 1 to 4 minutes (the first two library tracks by name,
  else the first two hits of a `The Beatles` search), seek item 1 to 20 s before its end, then the
  queue must advance to item 2 without going idle or paused, the capture must have no silence
  over 1 s until 3 s before the end and no gap of 3 s or more at the boundary, and the server log
  must have no `stopping queue` line. This is support#6860: with Autoplay OFF on the Spotify
  account, a Soloist engine reports `paused` at the end of every item.
- `external_pause` (sets with a `spotify` provider, skipped elsewhere): 10 s into a track the test
  sends `pause` on the engine's own WebSocket, as the Spotify app would. The queue must stop within
  3 s (first queue update with a resume position) and go idle on item 1 with that position kept
  (support#6752).
- `last_track_ends_clean` (every set): one track of 1 to 4 minutes, seeked to 20 s before its
  end. The queue must go idle without a `paused` queue or player state, the capture must have no
  silence over 1 s until 3 s before the end and no sound from 5 s after it (a replay), and the
  server log no ERROR line since the play (#6860, #6667, #6222).
- `provider_reload` (every set, once): reloads every non-builtin music provider, which must be
  available again within 60 s and return `The Beatles` tracks from its own domain (#6119). A
  server restart would cut the tests container's network, which it shares with the server.

Music tests carry no area marker: their `area` property is `music_<provider>` of the account
set (`ACCOUNT_SET_PROVIDERS` in `labctl/areas.py`), passed as `LAB_ACCOUNT_SET`.

The tracks used (`tracks` or `track`), the boundary gap (`boundary_gap_s`), the stop latency
(`stop_s`), the reloaded providers (`providers`) and their time to available (`available_s`) are
testcase properties in `junit.xml`, next to the checkpoints.

### Re-login

```
lab accounts reopen spotify-a [--ref dev] [--host-network]
```

starts a login instance on the set's existing golden, with the same options as `init` and the
same `password`. Redo the expired sign-in, then `lab accounts freeze spotify-a` again: it mints a
new token and leaves `lab.toml` as is. Reopen refuses while a run holds or waits for
`account:<set>`, and a music run on the set refuses while the set has a login instance.

### Remove a set

```
lab accounts remove spotify-a
```

stops the set's login instance if one runs, deletes `/srv/lab/accounts/<set>`, drops its line from
`lab.toml` and restarts labd. It refuses while a run holds or waits for `account:<set>`.

## Adding a player role

A role is `<kind>-<n>`, and Music Assistant must show it as an available player named `lab-<role>`. Bootstrap (`_wait_players`) and the `players` test fixture both find a role by that name, so a role that registers under another name fails the run. Verified flags for the existing roles are in [players.md](players.md); do not repeat them here.

1. Add the kind to `PLAYER_KINDS` in `labctl/config.py` and its provider domain to `PLAYER_DOMAINS` in `labctl/areas.py`. The domain becomes a valid `--providers` value and the kind part of the default list. Give it a journey in `scenarios/gate/test_player_kind.py` with a `provider_<domain>` marker, registered in `pyproject.toml`.
2. Add its compose service to `_player_service` in `labctl/runs.py`: image, command, mounts. The command must write 48 kHz 16-bit stereo PCM to `/fifo/pcm`, take the name `lab-<role>` from the player's own flag or hostname, and use an identifier seeded by `run.identity_scope` and the role (see the uuid5 for DLNA and the MAC for Squeezelite): unique per run, stable per account set in a music run so the golden does not collect a player config per run. `render_compose` then adds the rest: a `<role>-pacer` sidecar that creates the FIFO and stamps what the player writes, a per-role FIFO volume, and a `depends_on` on the healthy pacer so compose stops the player first. If the player writes faster than real time, add the kind to `UNPACED_KINDS` so its pacer drains at the PCM rate. A player that writes its own capture, like Sendspin, gets its own branch in `render_compose` instead.
3. Teach `labctl/bootstrap.py` about the provider: add `<kind>: <provider domain>` to `PROVIDER_DOMAINS` when the provider needs no setup values. Anything more, such as MPD's manual host list or a rename to `lab-<role>`, goes in `_add_player_providers` or a step of its own next to `_name_mpd_players`. Bootstrap already sets players with volume control to 100, because several providers start low enough to put the tone under the analyzer's silence floor.
4. Rebuild the players image when the role needs a new binary (add the package to `images/players/Dockerfile`) and after any change to the pacer, the MPD pipe copier or the Sendspin client, which the image installs as a copy and does not reload.

```
ssh lab 'cd /srv/lab/src && docker build -f images/players/Dockerfile -t lab/players:latest .'
```

5. Run the smoke test on the new kind. It is parametrized over every role, so no test change is needed. The capture must show tone `a1t1` at about -12 dB after play and silence within 2 s after stop.

```
lab run --ref dev
```

6. Add the verified command, provider setup and dead ends to `docs/players.md`, and extend the compose rendering tests in `tests/test_runs.py`.

## Adding a check

A check is one behaviour of the server, checked on one role or device, in a module of `scenarios/checks/`. Tests call checks; they do not hold the logic themselves.

- Signature `@check` + `async def <name>(ctx: CheckContext, role: str) -> None` (`device: str` for bench checks). `@check` from `scenarios/checks/context.py` names the checkpoints of its steps `<name>.<step>`.
- Wrap each phase in `with ctx.step("<step>", ISSUES):`, where `ISSUES` is a module constant with the support issues and PRs the check guards, such as `("#6860",)`. A failure records `failed_checkpoint` and adds the checkpoint to the error.
- Whatever the check starts, it stops in a `finally`, so a failed step does not leave a player playing into the next test.
- Timeouts and tolerances are module constants. Give each one that is not self-explanatory a one-line comment with the why, such as a measurement or the server behaviour it allows for.
- Record measurements with `ctx.record_property("<name>_s", value)`; the role is already in the node id.
- Give every check one deep test of its own in `scenarios/deep/test_<area>.py`, a one-liner on the area's roles via `pytest.mark.roles(*AREA_ROLES["<area>"])` (bench checks get a test in `scenarios/bench/`, music checks in `scenarios/music/`). Gate journeys in `scenarios/gate/` chain checks on the same roles.
- New roles for an area go in `AREA_ROLES` in `labctl/areas.py`, not in a test's marker.

Two unit tests enforce this: `tests/test_scenario_coverage.py` fails on a check without `@check` or without a deep, bench, music or smoke test that calls it, and `tests/test_gate_roles.py` fails when a gate module asks for roles outside `gate_roles` of its area.

## Hardware

Real speakers on the bench join a run with `--providers <device id>`, or `--providers devices` for all of them. Each run leases its devices exclusively, so a second run asking for a busy device queues while runs on other devices go ahead.

Prerequisites:

- The devices are on VLAN 50: the `MA Lab` SSID or a trunk port.
- Each device has an entry in `[devices]` in `config/lab.toml`: `<kind>-<n> = { provider = "<MA provider domain>", name = "<player name in MA>" }`, see `config/lab.example.toml`. A player is the device when its name equals `name` and its provider is that domain, an instance of it (`<domain>--<id>`), or one of its output protocols. The kind is free text but not a virtual player kind. Restart labd after a change (`sudo systemctl restart labd`), it reads the device lease names at start.
- `[hardware]` in `config/lab.toml` describes the macvlan network. IP plan on VLAN 50: subnet `10.50.0.0/24`, gateway `.1`, VM `.10`, shim `.11`, containers `.32-.63` (`ip_range`), UniFi DHCP pool `.100-.254`.

```
lab hw init      # once per VM: shim files under /etc/systemd/network, networkctl reload, docker network lab-hw
lab hw status    # checks the shim link is up and the network has the configured parent; lists the registry
```

`lab hw init` writes only files whose content changed and skips the network when it exists, so it is safe to repeat. To change the IP plan, remove the network first (`docker network rm lab-hw`, no run may be up) and run `lab hw init` again; `lab hw status` does not compare the network's addresses with `[hardware]`. A macvlan container cannot reach its host's parent address, so the VM gets the `lab-shim` interface with its own /32 and a route to the container range. A device run does the same checks before leasing and asks for `lab hw init` when one fails.

What a device run changes:

- The whole compose project joins the shared macvlan network `lab-hw` (external, so `docker compose down` leaves it). Several runs share that network, and Docker's DNS would answer a plain service name such as `ma` with any run's container, so every service also answers to `<project>-<service>` (`lab-<run id>-ma`) and the run's own containers use those names (`LAB_MA_URL`, Sendspin's `--server`, `LAB_RADIO_URL`).
- Virtual player providers other than `sendspin` are refused: AirPlay, DLNA and the others are discovered over the network, and two device runs would see each other's `lab-<role>` players. A music provider next to devices gets its own run after the device run, without the devices: the bench scenarios run on a fresh data dir, and bootstrap would otherwise write a device provider into the frozen account set.
- The server publishes no port. The VM reaches it at its container address (`docker inspect` of the `ma` container), and the server discovers the devices on the LAN.
- The tests container gets `LAB_DEVICES` (registry entries of the leased devices) and pytest runs `-m bench`, or `-m "smoke or bench"` when `sendspin` players are listed too: the play and stop test in `scenarios/bench/test_play_stop.py` (playing after play, idle after stop, on every leased device), the bench journeys and the state checks of [players.md](players.md#bench-devices). The state checks include one server restart, so a device run takes about 19 min for five devices.
- Each device adds a `device:<id>` lease. Gates and `--deep` do not collect `bench` scenarios, so `--areas`, `--all` and `--deep` refuse devices.

Without `sendspin` in `--providers` the device run has no virtual roles.

## Maintenance

Old runs and worktrees. `lab gc --days <n>` deletes run directories whose mtime is older than `n` days (it chowns each one back to `lab` first, because kept or failed runs leave root-owned files), then removes every worktree under `refs/` that no remaining run or account login instance uses; the next run recreates what it needs. It does that under the refs lock (`refs/.lock`), which runs and login instances hold from checkout until their `run.json` exists, so a worktree that is about to be used is never removed. The `lab-gc.timer` runs `labctl gc --days 14` daily as `lab` (`Persistent=true`, so a missed run happens at the next boot). `gc` is hidden from `lab --help`, because the timer is its caller.

```
ssh lab 'systemctl list-timers lab-gc.timer'
ssh lab 'journalctl -u lab-gc'
ssh lab 'docker ps'              # leftover lab-<run id> projects point at forgotten --keep runs
```

Images. The server image `lab/ma:<tag>` is rebuilt by every `lab run` with `--pull`, so a tag-only base such as `server:stable` moves to its latest release; it is a cached no-op unless the base or `images/ma/entrypoint.sh` changed. The players image is not: rebuild it after a change to `images/players/` (its first stage builds snapclient 0.31 from source with `snapclient-file-player-alive.patch`, see the Dockerfile; drop the stage once the snapclient package the image installs carries the fix), to `labctl/pacer.py` or `labctl/sendspin_client.py`, or to the dependencies in `pyproject.toml` (the tests run from the venv baked into it), otherwise the old code runs (the first AirPlay run after a pacer change died on a missing FIFO). Scenarios and `labctl` are copied from the synced repo into the run's `src/` when the run is created and need no rebuild.

Nightly digest. The nightly image is pinned in `[images] nightly` in `/srv/lab/src/config/lab.toml`. To move it, pull the tag, read the digest, edit the line, then prewarm:

```
ssh lab 'docker pull ghcr.io/music-assistant/server:nightly'
ssh lab "docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/music-assistant/server:nightly"
ssh lab 'nano /srv/lab/src/config/lab.toml'
lab run --ref dev
```

labd does not read `[images]`, so it needs no restart. Update the digest when a ref fails at server start with `this ref needs a newer base image` (its PyAV pin is newer than the one in the base image). The stable image follows the `stable` tag and every run pulls it.

Fresh data dir golden. `golden/tier1/` is built once, on the first run without a music provider, and is not rebuilt automatically. If a server change makes it stale, delete the directory and the next such run builds it again.

Known limitations:

- Nightly runs (a timer around `--deep`) and per-PR triggers (around `--areas`) are not built.
- The AirPlay role is pinned to `ap2_compat`. Native AirPlay 2 timing (PTP) stays silent against shairport-sync with nqptp in a container, and it is untested whether LAN shairport receivers behave the same.
- No role plays to Spotify Connect receivers, because Music Assistant has no provider for them. The Sendspin lab client has no volume control.
- Modules that import `labctl/seed.py` need `music_assistant_client`, which a venv on your machine lacks, so their unit tests only run on the VM.
