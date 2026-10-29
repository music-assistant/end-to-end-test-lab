#!/usr/bin/env bash
# Prepare a fresh Debian 13 VM as the lab host. Idempotent; run as root (sudo) from the
# synced repo: sudo /srv/lab/src/deploy/bootstrap-vm.sh
# The user must exist; override it with LAB_USER (the systemd units hardcode User=lab).
set -euo pipefail
LAB_ROOT=/srv/lab
LAB_USER=${LAB_USER:-lab}
SRC="$LAB_ROOT/src"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl gnupg git rsync python3 python3-venv qemu-guest-agent avahi-utils ffmpeg

# Docker CE from the official repo: Debian's docker.io lags and lacks the compose plugin.
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  DISTRO=$(. /etc/os-release && echo "$ID")
  curl -fsSL "https://download.docker.com/linux/$DISTRO/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$DISTRO $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
install -m 0644 "$HERE/daemon.json" /etc/docker/daemon.json
systemctl enable --now docker avahi-daemon
# OrbStack and Lima's VZ backend have no QEMU agent channel, so enabling it there would abort.
case "$(systemd-detect-virt || true)" in
  # a QEMU VM created without an agent channel cannot start it either
  qemu | kvm) systemctl enable --now qemu-guest-agent || true ;;
esac
systemctl restart docker
usermod -aG docker "$LAB_USER"

for d in runs refs golden accounts music/tones cache secrets; do
  install -d -o "$LAB_USER" -g "$LAB_USER" "$LAB_ROOT/${d%%/*}" "$LAB_ROOT/$d"
done
chmod 0700 "$LAB_ROOT/secrets"

# labctl venv lives next to the synced source so bin/lab can call it directly
if [ -f "$SRC/pyproject.toml" ]; then
  sudo -u "$LAB_USER" bash -c "cd '$SRC' && [ -x .venv/bin/python ] || python3 -m venv .venv"
  sudo -u "$LAB_USER" bash -c "cd '$SRC' && .venv/bin/pip install -q -e '.[test,scenarios]'"
fi

install -m 0644 "$HERE/labd.service" "$HERE/lab-gc.service" "$HERE/lab-gc.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now labd lab-gc.timer || true
echo "bootstrap done"
