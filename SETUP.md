# Setting up the lab from scratch

What the lab needs from your network and hypervisor, and the steps to build the VM, in the order
that works. Operating the lab afterwards (running tests, leases, account sets, adding a player
role) is in `docs/runbook.md`; per-player facts are in `docs/players.md`.

The lab is one Linux VM on an isolated network segment. Two rules shape every step: a lab player
or server must never reach your home LAN (real speakers answer mDNS and SSDP, and a lab run would
find and play to them), and your home LAN must still reach the VM (ssh, the browser for account
logins, a phone for Spotify pairing).

The values used in the reference installation, referred to below as "the example":

| What | Example value |
|---|---|
| Lab network | VLAN 50, 10.50.0.0/24, gateway 10.50.0.1 |
| Lab VM | 10.50.0.10, host name `lab`, user `lab` |
| Home LAN | 10.0.0.0/24 |
| Hypervisor | Proxmox VE 9 at 10.0.0.20 |
| Router and switches | UniFi (USG 3P gateway, Network application 10.6) |
| Wi-Fi for test devices | SSID `MA Lab` on the lab VLAN |

Pick your own addresses; `config/lab.toml` is where the VM address goes, nothing else in the repo
depends on them.

## What you need on your machine

- macOS or Linux with ssh, rsync and git.
- `bin/lab` on your PATH, or called by path.
- `config/lab.toml` kept locally next to the example. It is untracked.

Everything else runs on the VM. Python 3.12 or newer is only needed to run the lab's own unit tests in
`tests/`, not to use the lab:

```
pip install -e '.[test,scenarios]'
python -m pytest tests
```

## 1. Network

### What the lab needs

1. **A separate L2 segment** (a VLAN) with its own IPv4 subnet, a router address on it, and DHCP
   or a free address for the VM. mDNS and SSDP must not be bridged or reflected between this
   segment and your home LAN.
2. **Internet access** from the segment (NAT through your router). The VM pulls images and git
   refs, and music runs talk to the streaming services.
3. **A firewall rule** on the router: drop *new* connections from the lab segment to the home LAN,
   allow everything else. Established and related traffic must pass, so that connections your
   Mac or browser opens into the lab get their replies. The lab must not be able to open
   connections to any home device.
4. **A trunk to the hypervisor**: the switch port of the VM host carries the lab VLAN tagged, next
   to its own untagged management VLAN.
5. **Optional, for Spotify and hardware players: a Wi-Fi SSID on the lab segment.** Soloist
   pairing works from the Spotify app on a phone, which must discover the server over mDNS on the
   same segment. Real speakers added later join the same SSID.

mDNS reflection, "multicast DNS", "Bonjour gateway", "Avahi reflector" and similar features must
exclude the lab segment, or every lab player appears in your house.

### UniFi example

The example gateway runs the legacy LAN_IN rulesets (no zones). All of it can be done in the UI;
the REST bodies are given because that is how the example was built.

Network: Settings, Networks, New: name `Music Assistant Lab`, VLAN 50, host address
10.50.0.1/24, DHCP server on with range 10.50.0.100 to .254, DNS left to the gateway, mDNS
off, IGMP snooping off, IPv6 off, internet access on. The VM is given 10.50.0.10 statically,
outside the pool.

```json
POST /proxy/network/api/s/default/rest/networkconf
{"name": "Music Assistant Lab", "purpose": "corporate", "networkgroup": "LAN",
 "vlan_enabled": true, "vlan": 50, "ip_subnet": "10.50.0.1/24", "enabled": true,
 "is_nat": true, "internet_access_enabled": true,
 "dhcpd_enabled": true, "dhcpd_start": "10.50.0.100", "dhcpd_stop": "10.50.0.254",
 "dhcpd_leasetime": 86400, "dhcpd_dns_enabled": false, "dhcpd_gateway_enabled": false,
 "mdns_enabled": false, "igmp_snooping": false, "ipv6_enabled": false,
 "ipv6_interface_type": "none", "domain_name": "lab", "setting_preference": "manual"}
```

Firewall rule: Settings, Firewall, LAN In, new rule `Lab to LAN drop`, index 2100, action drop,
match state **new only**, source network `Music Assistant Lab`, destination network `Default`.
Through the API, `POST /rest/firewallrule` stored the rule with every state flag false, which
would also drop replies; a `PUT` of the same rule with `"state_new": true` and
`"setting_preference": "manual"` fixed it. Check the rule in the UI after creating it. Traffic to
the gateway's own LAN address is router-local and not covered by LAN_IN; that is harmless.

mDNS reflection: off on the Default network in the example. If you enable the reflector
site-wide, exclude the lab network.

Switch port: the example host hangs off a port with no port profile, which is the implicit "All"
profile (Default untagged, every VLAN tagged), so no port change was needed. A port with an
explicit profile needs the lab VLAN added.

Wi-Fi: Settings, WiFi, new: `MA Lab`, WPA2 personal, network `Music Assistant Lab`, all APs, both
bands, not a guest network, no client isolation (API: `POST /rest/wlanconf` copying
`ap_group_ids` and `usergroup_id` from an existing WLAN, `networkconf_id` set to the lab
network).

Give the gateway a few minutes to provision after each change. The gateway's network table must
list the lab network, and every AP must serve the SSID.

### Other routers

Any router that does VLANs with per-VLAN firewall rules works: OPNsense or pfSense (VLAN
interface, rule on the lab interface "block, source lab net, destination home net, direction in",
default pass to any), MikroTik, OpenWrt, a managed switch plus a Linux router. The check in
section 4 tells you whether the result is right, whatever made it.

## 2. Hypervisor

### What the lab needs

A VM with Debian 13, 4 vCPUs (8 for four concurrent runs) that expose at least the x86-64-v2 baseline (Python 3.14 in the
official image needs it: pass the host CPU type through, do not use a generic `qemu64` model),
8 GB RAM (16 GB for four runs), 64 GB disk (128 GB for four runs), one NIC on the lab VLAN, and cloud-init or an equivalent way to create user
`lab` with your ssh key and passwordless sudo (`labctl` restarts the broker through `sudo -n`
when an account set is frozen). The VM is the only thing on the lab segment; it needs no second
NIC. Docker, the players, the tests and the server all run inside it.

A bare-metal box on the lab VLAN works just as well; skip to section 3.

### Proxmox example

Make the bridge VLAN-aware so a VM NIC can carry a tag, keeping a backup for rollback:

```
cp -a /etc/network/interfaces /etc/network/interfaces.bak-ma-lab
# add to the vmbr0 stanza:
#     bridge-vlan-aware yes
#     bridge-vids 2-4094
ifreload -a
```

Existing guests stay untagged on PVID 1. Ping a few of them right after the reload and check
`bridge vlan show dev <uplink>` lists 1 untagged plus 2 to 4094; if something is unreachable,
restore the backup and reload again.

Create the VM from the Debian cloud image (pick a free VM id; the example used 107):

```
cd /var/lib/vz/template
wget https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
echo '<your ssh public key>' > /root/lab_ssh.pub

qm create 107 --name lab --memory 8192 --cores 4 --cpu host \
  --net0 virtio,bridge=vmbr0,tag=50 --scsihw virtio-scsi-single \
  --agent enabled=1 --ostype l26 --serial0 socket --vga serial0 --onboot 1
qm importdisk 107 debian-13-genericcloud-amd64.qcow2 local-lvm
qm set 107 --scsi0 local-lvm:vm-107-disk-0,discard=on,ssd=1 --boot order=scsi0 \
  --ide2 local-lvm:cloudinit --ciuser lab --sshkeys /root/lab_ssh.pub \
  --ipconfig0 ip=10.50.0.10/24,gw=10.50.0.1 --nameserver 10.50.0.1 --ciupgrade 0
qm resize 107 scsi0 64G
qm start 107
```

## 3. Reaching the VM

On the machine you run the lab from, add an ssh alias; `bin/lab` uses `lab` as the host name
(override with `LAB_SSH_HOST`):

```
Host lab
  HostName 10.50.0.10
  User lab
  IdentityFile ~/.ssh/<your key>
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 8
  StrictHostKeyChecking accept-new
```

`ssh lab 'ip -4 -br addr; sudo -n true && echo sudo-ok'` must answer within a minute of the VM
starting.

## 4. Isolation checks

Run these on the VM before anything else, and again after any change to the router, switch or
bridge. Replace the two home addresses with real hosts on your LAN (something that listens, such
as your Home Assistant box and the hypervisor):

```
curl -sI --max-time 8 https://deb.debian.org/ | head -1              # 200: egress works
getent hosts ghcr.io                                                   # resolves via the lab gateway
timeout 4 bash -c '</dev/tcp/<home host>/8095' || echo blocked         # must print blocked
timeout 4 bash -c '</dev/tcp/<hypervisor>/22'  || echo blocked         # must print blocked
ping -c1 <lab gateway>                                                 # answers
```

After section 5 also run, still on the VM:

```
avahi-browse -a -t                               # must list nothing from your house
docker run --rm alpine ping -c1 1.1.1.1          # container egress
```

If the blocked lines connect, or avahi-browse shows a speaker, stop here and fix the network.
Nothing else in this guide protects your speakers.

## 5. VM bootstrap

Run from the lab repo root on your machine. The cloud image has no rsync, so the first copy is a
tar pipe; `bin/lab` syncs with rsync from then on.

```
ssh lab 'sudo install -d -o lab -g lab /srv/lab /srv/lab/src'
tar --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
    --exclude 'config/lab.toml' -cf - . | ssh lab 'tar -xf - -C /srv/lab/src'
ssh lab 'sudo /srv/lab/src/deploy/bootstrap-vm.sh'
```

`deploy/bootstrap-vm.sh` is idempotent. It installs Docker CE with the compose plugin from
docker.com (Debian's docker.io lags), git, rsync, python3, avahi-utils, ffmpeg and the QEMU guest
agent (enabled only under QEMU or KVM); installs `deploy/daemon.json` (run bridges come from 10.200.0.0/16 in /24 slices, json-file
logs capped); adds `lab` to the docker group; creates the `/srv/lab` tree (`runs refs golden
accounts music/tones cache secrets`, secrets mode 0700); builds the labctl venv at
`/srv/lab/src/.venv` with the `test` and `scenarios` extras; installs and enables `labd.service`
(lease broker) and `lab-gc.timer` (daily cleanup). `labd` restart-loops until the config exists,
which the next step fixes.

## 6. Lab configuration

`config/lab.toml` is untracked and lives on the VM (keep a copy next to the example on your
machine; `bin/lab` never syncs it). Create it from the example, set `[vm] host` to the VM address
and fill in the nightly digest:

```
ssh lab 'docker pull ghcr.io/music-assistant/server:nightly && docker pull ghcr.io/music-assistant/server:stable'
ssh lab "docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/music-assistant/server:nightly"
sed 's|nightly@sha256:REPLACE_ME|nightly@sha256:<digest from above>|' config/lab.example.toml > config/lab.toml
# edit [vm] host if it is not 10.50.0.10
scp config/lab.toml lab:/srv/lab/src/config/lab.toml
ssh lab 'sudo systemctl restart labd'
bin/lab status            # capacity: 0/2 in use
```

Keys that matter: `[vm] host`, `[broker] capacity` (concurrent runs; 2 fits a 4 vCPU, 8 GB VM, 4 an 8 vCPU, 16 GB one: measured 2026-10-28 at about 0.3 core and 0.7 GB per run plus bootstrap bursts),
`[images] nightly` pinned by digest and `stable` by tag (every run pulls it), the `shairport` and
`potoken` sidecar images, `[secrets] app_vars_file`, and `[accounts]`, which `lab accounts
freeze` fills.

## 7. Images, mirror and tone library

```
ssh lab 'docker pull mikebrady/shairport-sync:latest && docker pull brainicism/bgutil-ytdlp-pot-provider:latest'
ssh lab 'cd /srv/lab/src && docker build -t lab/players:latest -f images/players/Dockerfile .'
ssh lab 'cd /srv/lab/src && .venv/bin/python -m tones.generate --out /srv/lab/music/tones'
```

`lab/players` (about 30 s, 1.4 GB) holds every virtual player and the capture tools; rebuild it
after changing its Dockerfile or `labctl/pacer.py`. The first run in step 8 clones the bare
mirror of music-assistant/server (about 700 MB), creates the `dev` worktree, downloads the
cliairplay binary the ref pins and pulls the base image. The lab server image `lab/ma:<tag>` is
built by every run. The tone library is 8 FLAC tracks, regenerated only when the generator changes.

## 8. First run

```
bin/lab run --ref dev                                  # smoke on every player kind, about 2 min cold
```

It must exit 0. It also clones the mirror and builds the golden of the fresh data dir (a throwaway
boot that creates the `lab` admin and mints a token into `/srv/lab/golden/tier1/`).

## 9. Account sets (music runs)

Music runs need the Music Assistant app vars file (the bundled client ids and keys for Spotify,
Apple Music, Tidal and Qobuz; core maintainers have the private `appvars` repo, everyone else
needs their own developer credentials in the same `{name: value}` JSON form) and one frozen
account set per streaming service you want to test.

The keys the server reads:

```
acoustid_api_key, apple_music_token, deezer_decrypt_key, fanarttv_api_key, lastfm_api_key,
lastfm_api_secret, qobuz_app_id, qobuz_app_secret, spotify_client_id, theaudiodb_api_key,
tidal_client_id_v2, tidal_client_secret_v2
```

Only the keys of the services you test are needed. The file never leaves the VM: it lives in
`/srv/lab/secrets` (mode 0700) and is not tracked.

```
scp app_vars.json lab:/srv/lab/secrets/app_vars.json
ssh lab 'chmod 600 /srv/lab/secrets/app_vars.json'
```

Then one login instance per set; each prints a URL on the lab VLAN address, user `lab` and a
password (also stored in `/srv/lab/accounts/<set>/password`):

```
bin/lab accounts init spotify-a --ref dev --host-network   # Soloist pairing needs mDNS from the phone
bin/lab accounts init apple-a
bin/lab accounts init tidal-a
bin/lab accounts init ytm-a                                # gets a potoken sidecar
bin/lab accounts init qobuz-a
bin/lab accounts init deezer-a
```

Open each URL from your home LAN, add exactly that one music provider and finish its sign-in.
Spotify: phone on the lab SSID, Soloist backend, pair from the Spotify app, Autoplay **off** on
the account (the #6860 scenario depends on it). YT Music: PO token server URL
`http://potoken:4416`. Then freeze each set, which mints its API token, stops the instance and
registers the set in `lab.toml`:

```
bin/lab accounts freeze spotify-a      # and so on for every set
bin/lab accounts status                # all frozen, no login
bin/lab run --ref dev --providers spotify
```

One set per service by choice: a login that expires is redone for that set alone, a broken
provider cannot fail another service's run, and sets lease independently.

## 10. Bench devices (optional)

Real speakers join the lab Wi-Fi SSID on the lab segment. Each one gets an entry in `[devices]` in
`config/lab.toml` (see `config/lab.example.toml`):

```
<kind>-<n> = { provider = "<MA provider domain>", name = "<player name in MA>" }
```

`[hardware]` describes the macvlan network on the VM's lab NIC. Copy the config to the VM and
restart the broker after every registry change. Set up the network once, check it, then do the
first device run:

```
scp config/lab.toml lab:/srv/lab/src/config/lab.toml
ssh lab 'sudo systemctl restart labd'
bin/lab hw init
bin/lab hw status
bin/lab run --ref dev --providers devices
```

Device runs need the VM's NIC on the lab segment (macvlan), so the bench is not available in the
laptop variant. The runbook's [Hardware](docs/runbook.md#hardware) section has the details.

## 11. Laptop variant (virtual players only)

The whole lab can run in a Linux VM on your own machine, with virtual players only. Every image
the lab uses is multi-arch (amd64 and arm64): the MA server images, `mikebrady/shairport-sync`,
`brainicism/bgutil-ytdlp-pot-provider` and `debian:trixie-slim`. The cliairplay release has an
aarch64 binary, and labctl picks the binary by host architecture.

Isolation holds because the players and the server sit on Docker bridge networks inside the VM,
which carry no multicast from the house. That needs NAT (shared) networking for the VM. Never
give it bridged networking.

A Lima recipe, untested on Apple silicon:

```
limactl create --name lab template://debian-13 --cpus 4 --memory 8 --disk 64
limactl start lab
limactl show-ssh --format config lab >> ~/.ssh/config
```

In the appended block, rename the `Host` to `lab` and set `User lab`. `deploy/bootstrap-vm.sh`
creates no user: it expects user `lab` with passwordless sudo. Create it in the VM and give it
the Lima user's authorized keys, so the key in the ssh block works for it:

```
limactl shell lab
# inside the VM:
sudo useradd -m -s /bin/bash lab && echo 'lab ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/lab
sudo install -d -m 0700 -o lab -g lab /home/lab/.ssh
sudo install -m 0600 -o lab -g lab "$HOME/.ssh/authorized_keys" /home/lab/.ssh/
```

Creating `lab` is the recommended path. To run as the Lima user instead, edit `User=` in
`deploy/labd.service` and `deploy/lab-gc.service` before section 5, and bootstrap with
`sudo LAB_USER=<lima user> /srv/lab/src/deploy/bootstrap-vm.sh`.

Then continue at section 5. Skip sections 1 and 2, and the LAN checks of section 4; keep the
egress and `avahi-browse` checks. Capacity 2 fits 4 vCPU and 8 GB.

Not available on a laptop:

- Bench devices (section 10): they need macvlan on the lab segment.
- Spotify Soloist pairing from a phone: mDNS does not pass the NAT. Skip `spotify-a`.

The other account sets work through the login URL. Set `[vm] host = "127.0.0.1"` in `lab.toml`:
the login instance then binds to the VM's loopback, which Lima forwards to the same port on your
machine, so the URL `bin/lab accounts init` prints opens as is. Untested.

## 12. What lives where

```
/srv/lab/src/            synced lab repo, .venv with labctl, config/lab.toml (untracked)
/srv/lab/server.git      bare mirror of music-assistant/server
/srv/lab/refs/<sha>/     one worktree per checked out sha (refs/.lock serializes creation)
/srv/lab/runs/<id>/      one directory per run: data/, captures/, src/, compose.yml, run.json, ma.log, junit.xml, summary.json
/srv/lab/golden/tier1/   seed settings.json, auth.db and token for runs without an account set
/srv/lab/accounts/<set>/ golden data dir, password, token, compose.yml of the login instance
/srv/lab/music/tones/    generated tone library, mounted read-only at /music
/srv/lab/cache/          uv cache (the only part mounted into servers) and cliairplay binaries
/srv/lab/secrets/        app_vars.json, mode 0700
/srv/lab/labd.sock       broker socket; /srv/lab/state.json its persisted leases and queue
```

Outside the VM: the VM definition on the hypervisor, the bridge backup if you made one, and the
network, firewall rule and SSID on the router.

## Tearing it down

Freeze or remove the account sets, destroy the VM, then remove the SSID, the firewall rule and
the lab network on the router, in that order. Your machine keeps the ssh alias and this repo.
