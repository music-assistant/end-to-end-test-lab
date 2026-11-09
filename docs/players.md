# Virtual player roles

Every role is `<kind>-<n>` and shows up in Music Assistant as `lab-<role>`. Verified on the lab VM
on 2026-10-27 against `dev` at 291ddf91 (nightly 2.11.0.dev2026102704 base) with
`lab run --ref dev` (the smoke test on every kind) and `lab run --ref dev --providers <domain>`
(the provider's journey).

All roles except Sendspin write 48 kHz 16-bit stereo into a FIFO at `/fifo/pcm` on a per-role
volume. A `<role>-pacer` sidecar (`lab-pacer --input /fifo/pcm`) creates the FIFO, stamps every
read and is the player's `depends_on` with `service_healthy` (healthy once the FIFO exists), so
compose stops the player first and the pacer finalizes after the last bytes. Bootstrap sets every
player that has volume control to 100 (unity gain): AirPlay starts at 20 and the Snapcast
builtin server at 25, which puts the tone below the analyzer's -60 dB silence floor.

Evidence: tone A (`a1t1`, 433.6 Hz, -12 dBFS) played on each role of one all-roles run
(20261027-095037-2c09), 8 s of play then stop. "After play" is the first tone sample after the
play command; "after stop" is the last non-silent sample after the stop command. The level is the
analyzer's peak for the tone segment. The mpd "after stop" is from 2026-10-29 with `lab-pipecopy`
(run 20261029-083614-f57b, 0.14 to 0.16 s over four stops; `dd` measured 0.76 to 0.81 s).

| Role | Tone found | Level | After play | After stop |
|---|---|---|---|---|
| sendspin | a1t1 | -12.0 dB | 0.36 s | 0.00 s |
| airplay | a1t1 | -12.0 dB | 0.62 s | 0.97 s |
| dlna | a1t1 | -12.0 dB | 0.41 s | 0.39 s |
| slim | a1t1 | -12.0 dB | 0.68 s | 0.42 s |
| snap | a1t1 | -12.0 dB | 1.07 s | 0.03 s |
| mpd | a1t1 | -12.0 dB | 0.12 s | 0.16 s |

## sendspin

`lab-sendspin --role sendspin-1 --dir /captures --server ma --identity <run id>-sendspin-1`
(`labctl/sendspin_client.py`, writes its own capture at the play-at time, no pacer). Provider:
builtin `sendspin`, nothing to set up; the client is auto-trusted through unpaired access. The
player has no volume control, so bootstrap skips it.

## sendspinviz

`lab-sendspin --role sendspinviz-1 ... --visualizer`: the same client with only the `visualizer`
role, as Hue Entertainment registers: no player role, no audio, no capture. It advertises
`visualizer_support` with a 2048-byte buffer and asks for beats, peaks and a 17-bin mel spectrum
(20 Hz to 20 kHz) at 20 Hz, the values of `hue_entertainment/bridge.py`. MA registers it as a
`SendspinVisualizerPlayer`, logged as `Player (type visualizer) registered: ...
/lab-sendspinviz-1`, hidden by default but in `players/all`. It can join a Sendspin syncgroup.
Not a `--providers` value and not in the smoke run: only the `sendspin` journey and the deep run
boot it (`JOURNEY_ROLES`, `DEEP_ROLES` in `labctl/areas.py`).

## airplay

Image `mikebrady/shairport-sync:latest` (5.6.2, AirPlay 2 build, `[images] shairport` in
`lab.toml`), command `-a lab-airplay-1`, with `images/shairport/shairport-sync.conf` mounted at
`/etc/shairport-sync.conf`: `output_backend = "pipe"`, pipe `name = "/fifo/pcm"`,
`output_rate = 48000`, `output_format = "S16_LE"`, `output_channels = 2`. The image entrypoint
starts dbus, avahi and nqptp; each container has its own IP on the run bridge, so no host
networking. Pacer without rate limit (shairport output is real time).

Provider: `airplay` (`config/providers/setup`, no values). MA discovers the receiver over mDNS,
names it after the advertised name and wraps it in a universal player `lab-airplay-1`. Bootstrap
pins `streaming_mode = ap2_compat` on the AirPlay protocol player (the `output_protocols` entry
with `protocol_domain` `airplay`); MA logs `Streaming to lab-airplay-1 via AirPlay 2
(raop-compat, NTP)`.

Did not work, same receiver, one play per mode:

- Automatic (`AirPlay 2 (buffered, PTP)`) and `ap2_ptp`: silent. MA warns `has not answered the
  server's PTP clock (0 clock exchange(s))`. tcpdump in the receiver's netns shows MA's
  Sync/Follow_Up/Announce/Signaling arriving unicast on 319/320 and nqptp answering only with
  Announce, never Delay_Req. shairport accepts the session, logs `NQPTP master clock
  d8cb39c291464f5a` and `Play started`, receives and deciphers buffered audio blocks, but never
  writes a frame to the backend until MA stops.
- `ap2_ntp`: shairport answers session SETUP with `501 Not Implemented` (no NTP timing for
  AirPlay 2 in shairport).
- `raop` (AirPlay 1) works as well and is the fallback if `ap2_compat` ever breaks.

Without the volume step the capture holds the tone at about -60 dB (MA's AirPlay default volume
20), which the analyzer counts as silence.

## dlna

`gmediarender -f lab-dlna-1 -u <uuid5 of run id and role> --logfile stdout --gstout-audiopipe
"audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! filesink
location=/fifo/pcm sync=true"` (Debian trixie gmediarender 0.3). `sync=true` makes the filesink
real time; the pipeline reopens the FIFO per track, which the pacer handles by reopening on EOF.
Pacer without rate limit.

Provider: `dlna` (`config/providers/setup`, no values). SSDP discovery on the run bridge finds the
renderer at provider load; MA names it after the friendly name (`-f`) and wraps it in a universal
player `lab-dlna-1`.

## slim

`sh -c "exec squeezelite -o - -a 16 -r 48000 -R -u X -s ma -m <locally administered MAC from run id and
role> -n lab-slim-1 > /fifo/pcm"` (Debian trixie squeezelite 2.0). squeezelite writes as fast as
its output drains and writes silence while idle, so the pacer runs with `--bytes-per-second
192000` (48000 x 2 x 2) and the FIFO back-pressure paces the player; measured 192023 B/s over
180 s. MA did not report stalls. `-R -u X` resamples to 48 kHz: announcements render at 44.1 kHz
(`controllers/streams/announcements.py`), and without it squeezelite wrote 44.1 kHz PCM that the pacer
read as 48 kHz (clip pitched up by 1.088, found 2026-10-27). `-R` alone keeps 44.1 kHz, because
squeezelite only resamples to a multiple of the source rate unless `X` (async) is set.

Provider: `squeezelite` (`config/providers/setup`, no values). The player connects to `ma:3483`
itself; MA takes the name from `-n`.

## snap

`snapclient --player file:filename=/fifo/pcm --hostID lab-snap-1 -h ma`, container `hostname:
lab-snap-1` (snapclient 0.31 built from source with `images/players/snapclient-file-player-alive.patch`:
the stock file player ticks on a destroyed player at every stream change and hangs or crashes). The file
player is real time and writes silence while idle (measured 191960 B/s); pacer without rate limit.

Provider: `snapcast` (`config/providers/setup`, no values). The official image ships `snapserver`,
so the provider defaults to its builtin server on 1704/1705. MA names the client after the
snapserver's `config.name` or else `host.name`, which is the container hostname, so the hostname is
set as well; the player id comes from `--hostID` (`ma_labsnap1`).

Observed on dev, not lab specific: MA logs `Finished Queue Flow stream` about 30 ms after
`Start Queue Flow stream` for the Snapcast queue while the tone plays for the full 8 s, and
`End of queue reached` about 5 s after the stop.

## mpd

`mpd --no-daemon --stderr /etc/mpd.conf` (Debian trixie MPD 0.24.4) with
`images/players/mpd.conf` mounted: no library, `bind_to_address "0.0.0.0"`, port 6600, one `pipe`
output with `command "exec lab-pipecopy /fifo/pcm"`, `format "48000:16:2"`,
`mixer_type "software"` (so MA's volume commands work). The pipe output writes as fast as it can,
so the pacer runs with `--bytes-per-second 192000`. `lab-pipecopy` (`labctl/pipecopy.py`) shrinks
its stdin pipe and the FIFO to one page, so after a stop or a stream change only about 0.15 s of
the old audio is left between MPD and the pacer.

Provider: `mpd` (`config/providers/setup`, then `config/providers/save` with
`manual_discovery_ip_addresses = ["mpd-1:6600"]`, the compose service name). MA registers
`mpd_mpd-1_6600` named `MPD (mpd-1)`; bootstrap renames it to `lab-mpd-1` with
`config/players/save values={"name": "lab-mpd-1"}`. MA serves MPD an MP3 flow stream.

Did not work as well:

- `command "cat > /fifo/pcm"`: about 1.1 s of tone after stop, the content of cat's 128 KiB buffer
  drained at the paced rate.
- `command "exec dd of=/fifo/pcm bs=4096 status=none"`: 0.76 to 0.81 s after stop, the two 64 KiB
  pipes (MPD to dd, dd to the pacer) drained at the paced rate; next played the skipped track
  for 1.8 s.
- `fifo` output instead of `pipe` (MPD paces itself, pacer without rate limit): 0.0 to 0.07 s after
  stop, but one of two plays had a 0.09 s `unknown` window inside the tone. That output drops data
  when the FIFO is full, so a capture can lose audio; kept the lossless pipe output.

# Bench devices

Real speakers on the bench, leased per run. The bench journeys in `scenarios/bench/test_bench.py`
run only with `lab run --providers <id>,<id>` or `--providers devices`: the registry is `[devices]`
in `config/lab.toml`, `lab hw init` sets the devices up, and the run leases them and adds a macvlan
network so MA reaches them on the LAN. Entry format and the network setup are in the runbook's
[Hardware](runbook.md#hardware) section. The journey runs once per leased device; with none leased
it is skipped.

The table below is the reference bench this lab was built with. Any registered device works; the
registry decides what runs.

Measured on the bench 2026-10-28 (dev at `8192482d`, run `20261028-095230-0310`), all five devices
in one run with `--providers devices`, the nightly shape: play and stop, bench journeys and state
checks, wall clock 1110 s. Play to `playing` and stop to `idle` from `test_play_stop.py`, the
journey from `test_bench.py`, the state checks from `test_state.py` (long pause is about 70 s on
every device, the rest 2 to 14 s each; the Sonos group 3 s; the restart 19.5 s with every device
back 11 to 16 s after the restart command):

| Device | play -> playing | stop -> idle | journey | state checks |
|---|---|---|---|---|
| `homepod-1` HomePod mini | 2.0 s | 0.01 s | 97 s | 120 s |
| `nestmini-1` Nest Mini | 2.1 s | 0.03 s | 105 s | 117 s |
| `sonos-1` Sonos Play:1 | 0.35 s | 0.25 s | 91 s | 111 s |
| `sonos-2` Sonos Play:1 | 0.2 s | 0.25 s | 91 s | 108 s |
| `wiim-1` WiiM Ultra | 0.06 s | 0.01 s | failed `position_not_past_end` (#6839, see below) | 107 s |

The WiiM failure in that run came from a source copy that predates the `bench_stop_position` fix
of 2026-10-28 (positions counted only for the tested item); with the fix the journey passes.

The WiiM Ultra (firmware Linkplay.5.2.829661) reported an elapsed time of 59.8 s after the 10 s
`a3t1` ended, which is the length of the `a1t4` track the seek check played just before: the
same stale-position shape as #6839. Real devices bootstrap at volume 1: nothing listens to them,
so the level only matters for the people in the room.

Real devices have no capture, so the checks in `scenarios/checks/bench.py` assert queue state,
`elapsed_time` and position only. Margins are wider than on the virtual players: 20 s to start
and 20 s past each track's length to advance or end.

Every device runs the same journey: album, pause and resume, seek, stop at end position, next
skips one. The lowest-named device of a provider with a second one leased also runs group skip
with that sibling. The checks are provider agnostic; the issues they came from (#6839 on WiiM,
server#7147 on Cast, server#6611 on Sonos, #6672 on AirPlay 2) are tagged on the steps.

What each check asserts:

- `bench_album`: the 10 s album `a3t1 a3t2 a3t3` starts, reaches index 1 and 2, leaves playing
  with `elapsed_time` within the last track, and reports the `a3t2` title at index 1.
- `bench_pause_resume`: `a1t4` paused after 5 s leaves `playing`, is still not playing 15 s
  later, and 2 s after resume reports an elapsed time 1 s before to 6 s after the paused one.
- `bench_seek`: a seek to 40 s in `a1t4` reports 39 to 48 s within 8 s, and the track ends 12 to
  35 s after the seek (no restart from 0, no early end).
- `bench_stop_position`: `a3t1` played to its end never reports an elapsed time or resume
  position over 1 s past its length, and a replay reports under 5 s elapsed 2 s after it plays.
- `bench_next_skips_one`: next 3 s into the album reaches index 1 and not index 2 within 5 s.
- `bench_group_skip`: with the second device grouped, next reaches index 1, both players report
  playing and the member stays synced to the leader.

`scenarios/bench/test_state.py` runs the state checks below, one test per check, on every leased
device; the group check only on Sonos and the restart check once. They carry the same `bench`
marker, so they run in the nightly routine's device run and in per-PR device runs.
Devices bootstrap at volume 1 and the volume checks stay at 3 or below, then set 1 again.

- `bench_volume_reported`: with `a1t4` playing, volume 3 is reported within 10 s, then volume 1.
  A device without `VOLUME_SET` skips the test (`<device> has no VOLUME_SET`).
- `bench_mute`: with `a1t4` playing at volume 3, mute is reported within 10 s and unmute reports
  unmuted at volume 3. A device without `VOLUME_SET` or `VOLUME_MUTE` skips the test.
- `bench_volume_kept`: volume 3 is still reported 1 s after the album reports playing and 1 s
  after each of pause, resume and next, recorded as `volume_after_<command>`. Same
  `VOLUME_SET` skip.
- `bench_long_pause`: `a1t4` paused after 5 s leaves `playing`; the queue is `idle` within 40 s
  (`idle_after_s`: at once for a stop-style pause, the player queue controller's 30 s limit for a
  held one), still `idle` 60 s after the pause, and resume plays on from the paused position
  within the `bench_pause_resume` window.
- `bench_stop_then_play`: stop 5 s into `a1t4` reports the queue `idle` within 10 s
  (`stopped_s`), and a new play reports under 5 s elapsed 2 s after it plays.
- `bench_radio_starts`: the lab radio's ICY stream plays within 15 s (`radio_playing_s`) and the
  queue still reports playing 10 s later.
- `bench_group_state` (`test_sonos_group`, the first leased Sonos as leader, skipped with one): both Sonos report playing within 15 s of
  `a1t4` starting, next reaches index 1 with both playing and the member still synced, and
  ungroup leaves the member idle and unsynced while the leader plays on.
- `bench_restart_rediscovers` (`test_restart_rediscovers`, once per run, needs the docker socket of
  the run): after a server restart every leased device is available within 90 s of the server
  answering again, recorded per device as `rediscovered_s_<id>` counted from the restart command,
  and the first device plays `a1t4`.

To add a family: add a test with `@pytest.mark.devices("<provider>")` (the registry provider key) to
`scenarios/bench/test_bench.py` that takes `device` and `ctx` and passes the `bench_*` checks that
fit the device to `_journey`.
