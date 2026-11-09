# PR gate

Which lab runs a server diff needs, and what they check. Agents read this before marking a server
PR ready.

Paths are relative to `server/music_assistant/`. Rows are matched top to bottom, the first match
wins. A path that matches no row runs `core` only. A row with areas and a `--providers` entry is
two runs, for example `lab run --ref pr/<n> --areas streams` and
`lab run --ref pr/<n> --providers squeezelite`: next to `--areas`, `--providers` only picks the
players the areas run on.

## Path to area

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
| `providers/<name>/**` of type `music` | music run on the matching frozen set: `lab run --ref pr/<n> --providers <domain>` with `spotify`, `apple_music`, `tidal`, `ytmusic`, `qobuz` or `deezer`. Other music providers have no lab coverage. |
| `providers/<name>/**` of type `plugin` | no lab area |
| `models/**` | every area and player journey: `lab run --ref pr/<n> --all` |
| `mass.py`, `__main__.py`, `constants.py`, `controllers/webserver/**`, `controllers/config/core.py`, `controllers/config/migrations.py`, `controllers/config/providers.py`, `controllers/cache/**`, `controllers/tasks/**`, `controllers/discovery/**`, `controllers/dashboard/**`, `helpers/webserver.py`, `helpers/database.py`, `helpers/config_entries.py`, `helpers/aiohttp_client.py`, `helpers/logging.py`, `helpers/security.py`, `helpers/jwt_auth.py`, `helpers/oauth.py`, `helpers/pkce.py`, `helpers/ssl.py`, `helpers/webrtc_certificate.py`, `helpers/app_vars.py`, `helpers/redirect_validation.py`, `helpers/guest_access.py` | `core` |
| anything else | `core` |

## Commands

```
lab run --ref pr/<n> --areas <a>[,<b>]     # core is always added; roles come from the areas
lab run --ref pr/<n> --providers <player domain>   # core plus that provider's journey
lab run --ref pr/<n> --providers <music domain>    # music run on its frozen account set
lab run --ref pr/<n> --all                 # every area and journey on every player kind
lab summary <run id>                       # failures per area with checkpoint and issues
lab runs --ref pr/<n>                      # the PR's runs: purpose, exit code, rerun
lab run --ref dev --deep                   # daily deep run, not a PR check
lab run --ref dev --deep --providers slim,slim -k test_member_rejoins   # one deep test, no account sets
lab run --ref pr/<n> --providers devices   # bench play and stop, journeys and state checks on every registered device
```

A gate (`--areas`, `--all`, or player providers alone) that fails is rerun once in a fresh
project. It is red only when a test fails twice, and exits 0 when green.

Examples:

- `controllers/player_queues/controller.py` changed: `lab run --ref pr/<n> --areas queue`
- `controllers/players/announcements.py` changed: `lab run --ref pr/<n> --areas players,streams`
- `providers/wiim/player.py` changed: `lab run --ref pr/<n> --providers wiim-1`

## What each area checks

The checks are provider agnostic on purpose: a player that behaves differently is fixed in its
provider, never in a check. They assert behaviour, not labels: a pause leaves `playing`, goes
silent and resumes near where it paused, whether the player pauses in place or stops and restarts.

- `core`: `boot_clean` (providers loaded, roles registered, no error at boot) and `album_plays_through` on `slim-1`.
- `queue`: `album_plays_through`, `resume_advances`, `now_playing_follows` on airplay, dlna, slim, snap and sendspin.
- `streams`: `flow_continuous`, `flow_next_skips_one`, `crossfade_overlap`, `seek_resumes`, `elapsed_tracks_pause`, `stop_after_end_position`, `short_file_plays`, `radio_start_latency`, `radio_icy_plays`, `radio_hls_plays`, `radio_403_rejected` on slim and sendspin.
- `players`: group, announce, volume and pause checks on `slim-1` and `slim-2`.
- `library`: `scan_matches_manifest`, `rows_survive_restart`.
- `--providers <domain>`: album, resume, next drops the previous track, volume when the player advertises it, and announce on that provider's one role (`gate/test_player_kind.py`, marker `provider_<domain>`, area `<domain>` in `lab summary`).
- `--providers sendspin` also boots `sendspin-2` and the visualizer-only client `sendspinviz-1` (a stand-in for Hue Entertainment) and runs `leader_removed_with_visualizer_keeps_playing`: a dynamic syncgroup of `[sendspin-1, sendspinviz-1, sendspin-2]` plays, `sendspin-1` is removed from its members, and `sendspin-2` must stay free of silence of 1 s or more for 10 s with the group queue still playing.

Every check records the support issue ids it covers as JUnit properties. `lab summary` prints them for
failed tests.
