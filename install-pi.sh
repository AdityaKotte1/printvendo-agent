#!/usr/bin/env bash
# Set a Raspberry Pi up as a Printvendo kiosk, in one command.
#
#   curl -fsSL https://.../install-pi.sh | sudo bash -s -- --code dve_xxxx
#
# Written for somebody SSHed into a headless Pi on a shop's wifi: it installs
# what is missing, enrols the machine, and leaves a service that starts itself
# at boot. It says what it is doing at each step, because a script that goes
# quiet for four minutes on a slow connection looks hung.
set -euo pipefail

API="https://api.printvendo.com"
CODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CODE" ]]; then
  echo "Usage: sudo bash install-pi.sh --code dve_..." >&2
  echo "Get the code from the Printvendo admin console, for this shop." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: it installs a service." >&2
  exit 1
fi

echo "==> Installing what is needed (python, CUPS, ghostscript)"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv cups ghostscript

echo "==> Checking the printer"
# CUPS must already have the printer. Adding it needs a human who can see the
# model, and guessing here is how a dissertation prints on a label machine.
if ! lpstat -e | grep -q .; then
  echo "No printer is set up in CUPS yet." >&2
  echo "Add it first (usually: plug it in and run \`lpadmin\`, or open http://localhost:631)." >&2
  exit 1
fi
lpstat -e | sed 's/^/    /'

echo "==> Installing the agent"
install -d /opt/printvendo
python3 -m venv /opt/printvendo/venv
/opt/printvendo/venv/bin/pip install --quiet --upgrade pip
/opt/printvendo/venv/bin/pip install --quiet "$(dirname "$0")"

echo "==> Enrolling this machine"
/opt/printvendo/venv/bin/printvendo-agent enrol --code "$CODE" --api "$API"

echo "==> Installing the service"
cat > /etc/systemd/system/printvendo-agent.service <<'UNIT'
[Unit]
Description=Printvendo print agent
# Printing needs CUPS, and a kiosk with no network is a kiosk with no jobs.
After=network-online.target cups.service
Wants=network-online.target

[Service]
ExecStart=/opt/printvendo/venv/bin/printvendo-agent run
Restart=always
# A shop's wifi drops. Restarting immediately and for ever would spin; five
# seconds is long enough to matter and short enough that nobody notices.
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now printvendo-agent

echo
echo "Done. The kiosk is printing."
echo "  Check it:   /opt/printvendo/venv/bin/printvendo-agent check"
echo "  Watch it:   journalctl -u printvendo-agent -f"
