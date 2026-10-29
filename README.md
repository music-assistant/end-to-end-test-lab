# Music Assistant end-to-end test lab

One command runs a Music Assistant ref against a fresh server plus virtual players on an isolated
Docker network, captures what every player received, and returns pass/fail with JUnit XML, the MA
log and the PCM captures. It runs in one Linux VM on an isolated network segment, or in a laptop VM
for the virtual tier, so no lab player can reach a real speaker.

```
lab run --ref pr/7244 --areas queue        # PR gate: core plus the queue area
lab run --ref pr/7244 --providers airplay  # core plus the AirPlay journey
lab run --ref pr/7244 --providers spotify  # tier 2 scenarios on the Spotify account set
lab run --ref pr/7244 --providers devices  # real speakers on the bench
lab run --ref dev                          # smoke test on one player of each kind
lab runs                                   # recent runs with their purpose and exit code
lab status                                 # leases and queue
```

Who it is for: server contributors and maintainers who want end-to-end evidence for a PR or a
nightly, agents that gate PRs (see [Which areas for my diff](#which-areas-for-my-diff)), and anyone
reproducing a player bug against virtual or bench players.

## Layout

| Path | Purpose |
|---|---|
| `labctl/` | broker (`labd`), CLI (`labctl`), leases, refs, run lifecycle, bootstrap, pacer, Sendspin client, analyzer |
| `images/ma/` | lab server image: official image + git, entrypoint reconciles requirements and runs the mounted checkout |
| `images/players/` | one image with every virtual player (gmediarender, squeezelite, snapclient, mpd, aiosendspin client), plus the MPD config |
| `images/shairport/` | config mounted into the official shairport-sync image for the AirPlay role |
| `tones/` | tone library generator and manifest |
| `scenarios/` | pytest scenarios: `checks/` (one body per check), `gate/` (per-area journeys), `deep/` (one check per test), `tier1/`, `tier2/`; markers `gate`, `deep`, `area_*`, `provider_*`, `roles`, `tier1`, `tier2`, `hw` |
| `tests/` | unit tests for the lab code itself (synthetic data only) |
| `config/` | `lab.example.toml` (tracked) and `lab.toml` (untracked, house-specific) |
| `deploy/` | VM bootstrap, systemd unit, docker daemon config |
| `docs/` | runbook and per-player verified flags |

House-specific values (IPs, account dirs, secrets) live only in `config/lab.toml` and
`/srv/lab/secrets/` on the VM.

## Which areas for my diff

Agents use this section before marking a server PR ready. Paths are relative to
`server/music_assistant/`. Rows are matched top to bottom, the first match wins. A path that matches
no row runs `core` only. A row with areas and a `--providers` entry is two runs, for example
`lab run --ref pr/<n> --areas streams` and `lab run --ref pr/<n> --providers squeezelite`: next to
`--areas`, `--providers` only picks the players the areas run on.

### Path to area

| Path | Area |
|---|---|
| `controllers/players/announcements.py`, `controllers/players/audio_sources.py` | `players`, `streams` |
| `controllers/player_queues/stream_feeder.py` | `queue`, `streams` |
| `helpers/audio.py` | `streams`, `queue`, `players` |
| `helpers/ffmpeg.py`, `helpers/dsp.py`, `controllers/config/dsp.py` | `streams`, `players` |
| `helpers/playlists.py` | `library`, `streams` |
| `controllers/storage/**` | `library` |
| `helpers/hls.py` | `music:bbc_sounds`, `music:nicovideo` (no lab area) |
| `providers/sync_group/**`, `providers/universal_group/**`, `providers/universal_player/**`, `controllers/config/players.py` | `players` |
| `providers/squeezelite/multi_client_stream.py` | `--providers squeezelite`, `streams` |
| `providers/snapcast/ma_stream.py` | `--providers snapcast`, `streams` |
| `providers/sendspin/playback.py` | `--providers sendspin`, `streams` |
| `providers/airplay/stream.py` | `--providers airplay` |
| `controllers/config/queues.py`, `controllers/player_queues/**` | `queue` |
| `controllers/streams/**`, `helpers/mp3.py`, `helpers/buffered_generator.py`, `helpers/named_pipe.py`, `providers/smart_fades/**`, `providers/loudness_analysis/**` | `streams` |
| `controllers/players/**`, `helpers/player.py`, `helpers/virtual_player.py`, `helpers/wake_on_lan.py` | `players` |
| `controllers/music/**`, `controllers/metadata/**`, `controllers/config/filesystem_consolidation.py`, `helpers/mount.py`, `helpers/tags.py`, `helpers/images.py`, `helpers/lyrics.py`, `helpers/compare.py`, `helpers/external_ids.py`, `helpers/track_filter.py`, `helpers/podcast_parsers.py`, `providers/filesystem_local/**`, `providers/filesystem_cloud/**`, `providers/filesystem_google_drive/**`, `providers/filesystem_onedrive/**`, `providers/filesystem_yandex_disk/**`, `providers/builtin/**`, `providers/music247e/**`, `providers/sonic_analysis/**`, `providers/acoustid_lookup/**`, `providers/<name>/**` of type `metadata` or `audio_analysis` | `library` |
| `providers/sendspin/**` | `--providers sendspin` |
| `providers/airplay/**` | `--providers airplay`, plus the HomePod device run |
| `providers/dlna/**` | `--providers dlna` |
| `providers/squeezelite/**` | `--providers squeezelite` |
| `providers/snapcast/**` | `--providers snapcast` |
| `providers/mpd/**` | `--providers mpd` |
| `providers/{chromecast,sonos,wiim}/**` | device run on the matching bench device: `lab run --ref pr/<n> --providers <id>` with `nestmini-1`, `sonos-1` or `wiim-1`; `lab hw status` lists the registry |
| `providers/{alexa,amplipi,bluesound,bose_soundtouch,fully_kiosk,hass_players,heos,local_audio,msx_bridge,musiccast,raumfeld,roku_media_assistant,samsung_wam,sonos_s1,yandex_station}/**` | no lab coverage, write that in the PR |
| `providers/<name>/**` of type `music` | tier 2 on the matching frozen set: `lab run --ref pr/<n> --providers <domain>` with `spotify`, `apple_music`, `tidal`, `ytmusic`, `qobuz` or `deezer`. Other music providers have no lab coverage. |
| `providers/<name>/**` of type `plugin` | no lab area |
| `models/**` | every area and player journey: `lab run --ref pr/<n> --all` |
| `mass.py`, `__main__.py`, `constants.py`, `controllers/webserver/**`, `controllers/config/core.py`, `controllers/config/migrations.py`, `controllers/config/providers.py`, `controllers/cache/**`, `controllers/tasks/**`, `controllers/discovery/**`, `controllers/dashboard/**`, `helpers/webserver.py`, `helpers/database.py`, `helpers/config_entries.py`, `helpers/aiohttp_client.py`, `helpers/logging.py`, `helpers/security.py`, `helpers/jwt_auth.py`, `helpers/oauth.py`, `helpers/pkce.py`, `helpers/ssl.py`, `helpers/webrtc_certificate.py`, `helpers/app_vars.py`, `helpers/redirect_validation.py`, `helpers/guest_access.py` | `core` |
| anything else | `core` |

### Commands

```
lab run --ref pr/<n> --areas <a>[,<b>]     # core is always added; roles come from the areas
lab run --ref pr/<n> --providers <player domain>   # core plus that provider's journey
lab run --ref pr/<n> --providers <music domain>    # tier 2 on its frozen account set
lab run --ref pr/<n> --all                 # every area and journey on every player kind
lab summary <run id>                       # failures per area with checkpoint and issues
lab runs --ref pr/<n>                      # the PR's runs: purpose, exit code, rerun
lab run --ref dev --deep                   # daily deep run, not a PR check
lab run --ref dev --deep --providers slim,slim -k test_member_rejoins   # one deep test, no account sets
lab run --ref pr/<n> --providers devices   # hw smoke, bench journeys and state checks on every registered device, about 19 min for five
```

A gate (`--areas`, `--all`, or player providers alone) that fails is rerun once in a fresh
project. It is red only when a test fails twice, and exits 0 when green.

Examples:

- `controllers/player_queues/controller.py` changed: `lab run --ref pr/<n> --areas queue`
- `controllers/players/announcements.py` changed: `lab run --ref pr/<n> --areas players,streams`
- `providers/wiim/player.py` changed: `lab run --ref pr/<n> --providers wiim-1`

### Measured gate timings on dev (2026-10-27)

Whole run, including boot (about 45 s) and the `core` journey (37 s):

| Area | Run | Journey |
|---|---|---|
| `core` | 37 s plus boot | one role |
| `queue` | 381 s | 64 to 74 s per role, 5 roles |
| `streams` | 510 s | slim 257 s, sendspin 184 s |
| `players` | 390 s together with the squeezelite journey | 239 s on slim-1 and slim-2 |
| `library` | 99 s | 42 s |
| `--providers <domain>` | 557 s for all six | 79 to 95 s per journey |

Known red on `dev` since 2026-10-28, until the server fix lands. The checks are provider
agnostic on purpose: a player that behaves differently is fixed in its provider, never in a check.
They assert behaviour, not labels: a pause leaves `playing`, goes silent within 2 s and resumes
within 1 s of where it paused, whether the player pauses in place or stops and restarts.

- `next_heard` of `next_drops_previous` on `airplay-1` (`--providers airplay`): the next track
  is heard 4.2 s after next, limit 3 s, and the skipped one can play on for 2.0 s (runs
  20261029-082916-1957, 083118-2c9a, 083357-21e3, 082635-e1e9): the receiver drains its 2 s
  RAOP buffer after the 1 s next debounce. Fix: the AirPlay provider flushes the receiver on a
  skip, and the queue drops its 1 s next debounce; the flush alone lands at about 3.2 s.

`core`, `queue`, `players` and `streams` on slim, airplay and dlna passed three gate runs first
time on 2026-10-28 (server 8192482d2314 and f4c51dc251c2). The Snapcast `advances` red of
2026-10-27 was server#7250.

### What each area checks

- `core`: `boot_clean` (providers loaded, roles registered, no error at boot) and `album_plays_through` on `slim-1`.
- `queue`: `album_plays_through`, `resume_advances`, `now_playing_follows` on airplay, dlna, slim, snap and sendspin.
- `streams`: `flow_continuous`, `flow_next_skips_one`, `crossfade_overlap`, `seek_resumes`, `elapsed_tracks_pause`, `stop_after_end_position`, `short_file_plays`, `radio_start_latency`, `radio_icy_plays`, `radio_hls_plays`, `radio_403_rejected` on slim and sendspin.
- `players`: group, announce, volume and pause checks on `slim-1` and `slim-2`.
- `library`: `scan_matches_manifest`, `rows_survive_restart`.
- `--providers <domain>`: album, resume, next drops the previous track, volume when the player advertises it, and announce on that provider's one role (`gate/test_player_kind.py`, marker `provider_<domain>`, area `<domain>` in `lab summary`).

Every check records the support issue ids it covers as JUnit properties. `lab summary` prints them for
failed tests.

## Setup and runbook

[`SETUP.md`](SETUP.md) builds the lab from scratch: what the network and hypervisor must provide
(with UniFi and Proxmox as examples), the VM, bootstrap, config, images, account sets, the
isolation checks, bench devices, and a laptop variant for the virtual tier.
[`docs/runbook.md`](docs/runbook.md) covers operating it: runs, leases, account sets, adding a
player role, maintenance. [`docs/players.md`](docs/players.md) has the verified flags per player.

## License

Apache 2.0, see [LICENSE](LICENSE).
