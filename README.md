# Music Assistant end-to-end test lab

One command runs a Music Assistant ref against a fresh server plus virtual players on an isolated
Docker network, captures what every player received, and returns pass/fail with JUnit XML, the MA
log and the PCM captures. It runs in one Linux VM on an isolated network segment, or in a laptop VM
with virtual players only, so no lab player can reach a real speaker.

```
lab run --ref pr/7244 --areas queue        # PR gate: core plus the queue area
lab run --ref pr/7244 --providers airplay  # core plus the AirPlay journey
lab run --ref pr/7244 --providers spotify  # music scenarios on the Spotify account set
lab run --ref pr/7244 --providers devices  # real speakers on the bench
lab run --ref dev                          # smoke test on one player of each kind
lab runs                                   # recent runs with their purpose and exit code
lab status                                 # leases and queue
```

Who it is for: server contributors and maintainers who want end-to-end evidence for a PR or a
nightly, agents that gate PRs, and anyone reproducing a player bug against virtual or bench players.

## Layout

| Path | Purpose |
|---|---|
| `labctl/` | broker (`labd`), CLI (`labctl`), leases, refs, run lifecycle, bootstrap, pacer, Sendspin client, analyzer |
| `images/ma/` | lab server image: official image + git, entrypoint reconciles requirements and runs the mounted checkout |
| `images/players/` | one image with every virtual player (gmediarender, squeezelite, snapclient, mpd, aiosendspin client), plus the MPD config |
| `images/shairport/` | config mounted into the official shairport-sync image for the AirPlay role |
| `tones/` | tone library generator and manifest |
| `scenarios/` | pytest scenarios: `checks/` (one body per check), `gate/` (per-area journeys), `deep/` (one check per test), `smoke/`, `music/`, `bench/`; markers `gate`, `deep`, `smoke`, `music`, `bench`, `area_*`, `provider_*`, `roles`, `devices` |
| `tests/` | unit tests for the lab code itself (synthetic data only) |
| `config/` | `lab.example.toml` (tracked) and `lab.toml` (untracked, house-specific) |
| `deploy/` | VM bootstrap, systemd unit, docker daemon config |
| `docs/` | runbook, per-player verified flags, PR gate map |

House-specific values (IPs, account dirs, secrets) live only in `config/lab.toml` and
`/srv/lab/secrets/` on the VM.

## Setup and runbook

[`SETUP.md`](SETUP.md) builds the lab from scratch: what the network and hypervisor must provide
(with UniFi and Proxmox as examples), the VM, bootstrap, config, images, account sets, the
isolation checks, bench devices, and a laptop variant for virtual players only.
[`docs/runbook.md`](docs/runbook.md) covers operating it: runs, leases, account sets, adding a
player role, maintenance. [`docs/players.md`](docs/players.md) has the verified flags per player.
[`docs/pr-gate.md`](docs/pr-gate.md) maps a server diff to the lab runs it needs and lists what
each area checks.

## License

Apache 2.0, see [LICENSE](LICENSE).
