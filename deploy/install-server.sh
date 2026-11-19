#!/usr/bin/env bash
# Install (or update) the tailclip relay on a Linux host running Tailscale.
# Usage: sudo ./deploy/install-server.sh [--no-serve]
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo $0" >&2
  exit 1
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
SOCK=/run/tailclip/tailclip.sock

install -d -m 0755 /opt/tailclip
install -m 0644 "$SRC/server/tailclip.py" "$SRC/server/index.html" /opt/tailclip/
install -m 0644 "$SRC/deploy/tailclip.service" /etc/systemd/system/tailclip.service
systemctl daemon-reload
systemctl enable tailclip.service >/dev/null
systemctl restart tailclip.service

for _ in $(seq 1 40); do
  [ -S "$SOCK" ] && break
  sleep 0.25
done
[ -S "$SOCK" ] || { echo "tailclip did not start; see: journalctl -u tailclip" >&2; exit 1; }
echo "tailclip is running on unix:$SOCK"

if [ "${1:-}" != "--no-serve" ]; then
  command -v tailscale >/dev/null || { echo "tailscale is not installed" >&2; exit 1; }
  # Private HTTPS for your tailnet only. This uses port 443 at the root path;
  # see README if this node already serves something else.
  tailscale serve --bg "unix:$SOCK"
  tailscale serve status
fi
