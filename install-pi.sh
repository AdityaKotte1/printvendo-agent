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
PRINTER=""
# A xerox counter runs several machines off one agent. Space-separated here
# because bash arrays through getopts are more trouble than they are worth for
# two flags; the agent takes them one --bw / --colour at a time.
BW=""
COLOUR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --printer) PRINTER="$2"; shift 2 ;;
    --bw) BW="$BW $2"; shift 2 ;;
    --colour|--color) COLOUR="$COLOUR $2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CODE" ]]; then
  echo "Usage: sudo bash install-pi.sh --code dve_... [--printer NAME] [--api URL]" >&2
  echo "  several machines: --bw 'Mono-1' --bw 'Mono-2' --colour 'Colour-1'" >&2
  echo "Get the code from the Printvendo admin console, for this shop." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: it installs a service." >&2
  exit 1
fi

# A Pi has no battery-backed clock. It learns the time from NTP at boot, so one
# that booted without network -- or on a shop wifi blocking UDP 123 -- sits at
# whatever time it last knew. Everything then fails at TLS with "certificate is
# not yet valid", and apt refuses its own signatures as "not live until", which
# reads like a broken mirror rather than a wrong clock. Checked here, before
# apt and the venv, because the alternative is finding out at the last step.
echo "==> Checking the clock"
if ! REMOTE=$(curl -sI --max-time 15 http://deb.debian.org/ | grep -i '^date:' | cut -d' ' -f2-); then
  echo "Could not reach the network to check the time. Fix the connection first." >&2
  exit 1
fi
DRIFT=$(( $(date -u +%s) - $(date -u -d "$REMOTE" +%s) ))
if [[ ${DRIFT#-} -gt 60 ]]; then
  echo "This Pi's clock is out by ${DRIFT} seconds." >&2
  echo "  it thinks:  $(date -u)" >&2
  echo "  really is:  $REMOTE" >&2
  echo >&2
  echo "TLS will refuse the API certificate and apt will refuse its own" >&2
  echo "signatures until this is right. Fix it with:" >&2
  echo "    sudo timedatectl set-ntp true" >&2
  echo "    sudo systemctl restart systemd-timesyncd" >&2
  echo "If that does not work, this wifi is probably blocking NTP. Set it by" >&2
  echo "hand and try again:" >&2
  echo "    sudo date -s '$REMOTE'" >&2
  exit 1
fi
echo "    clock is right, within ${DRIFT#-}s"

echo "==> Installing what is needed (python, CUPS, ghostscript)"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv cups ghostscript

echo "==> Checking the printer"
# CUPS must already have the printer. Adding it needs a human who can see the
# model, and guessing here is how a dissertation prints on a label machine.
if ! lpstat -e | grep -q .; then
  echo "No printer is set up in CUPS yet." >&2
  echo >&2
  echo "On a headless Pi, with the printer plugged in and switched on:" >&2
  echo "    lpinfo -v                      # find its usb:// address" >&2
  echo "    lpadmin -p Shop -E -v 'usb://...' -m everywhere" >&2
  echo "    lpstat -e                      # it should now be listed" >&2
  echo >&2
  echo "\`-m everywhere\` is driverless IPP and is right for almost any printer" >&2
  echo "made since about 2015. For an older one, \`lpinfo -m | grep -i <model>\`" >&2
  echo "lists the drivers this Pi has." >&2
  exit 1
fi
lpstat -e | sed 's/^/    /'

# CUPS's default error policy is `stop-printer`, and this agent depends on it.
# A jam then holds the job and marks the printer `disabled`, which is what the
# queue watcher reads to raise PrinterStuck, close the shop and stop it taking
# money for prints that cannot come out.
#
# `abort-job` looks kinder and is the opposite: CUPS discards the failed job and
# carries on, so it vanishes from `lpstat -o` exactly as a finished one does.
# The agent cannot tell them apart, reports the job printed, and the student is
# charged for paper that never moved. That was set on three live kiosks and
# seventy-four jobs were reported printed while the shop produced nothing.
if [[ -n "${PRINTER}" ]]; then
  CURRENT=$(lpoptions -p "$PRINTER" 2>/dev/null | tr ' ' '
' | grep '^printer-error-policy=' || true)
  if [[ "$CURRENT" == *abort-job* || "$CURRENT" == *retry-job* ]]; then
    echo "==> Putting $PRINTER back on stop-printer (was ${CURRENT#*=})"
    lpadmin -p "$PRINTER" -o printer-error-policy=stop-printer
  fi
fi

FOUND=$(lpstat -e | wc -l)
if [[ -n "$PRINTER" ]]; then
  if ! lpstat -e | grep -qx "$PRINTER"; then
    echo "There is no printer called '$PRINTER' on this Pi. Use one of the names above." >&2
    exit 1
  fi
elif [[ "$FOUND" -gt 1 && -z "$BW" && -z "$COLOUR" ]]; then
  # Guessing between two printers means somebody's dissertation on the label
  # machine.
  echo "This Pi has $FOUND printers. Run again with --printer NAME," >&2
  echo "or split them: --bw 'Mono-1' --bw 'Mono-2' --colour 'Colour-1'" >&2
  exit 1
fi

echo "==> Installing the agent"
install -d /opt/printvendo
python3 -m venv /opt/printvendo/venv
/opt/printvendo/venv/bin/pip install --quiet --upgrade pip
/opt/printvendo/venv/bin/pip install --quiet "$(dirname "$0")"

echo "==> Enrolling this machine"
ENROL=(enrol --code "$CODE" --api "$API")
[[ -n "$PRINTER" ]] && ENROL+=(--printer "$PRINTER")
for NAME in $BW; do ENROL+=(--bw "$NAME"); done
for NAME in $COLOUR; do ENROL+=(--colour "$NAME"); done
/opt/printvendo/venv/bin/printvendo-agent "${ENROL[@]}"

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
systemctl enable printvendo-agent
# `enable --now` starts a service that is stopped and does nothing at all to one
# that is already running. On a re-install that mattered: enrolling rotates the
# token on the kiosk's existing device row, so the server had just invalidated
# the token the running process was holding in memory. It went on polling with
# it and got 401 on every call for ever, while `check` below read the *file* --
# which has the new token -- and reported the kiosk healthy. A shop stopped
# printing and the installer said it was fine.
systemctl restart printvendo-agent

echo "==> Checking it end to end"
# The installer says whether the shop can print, rather than leaving somebody
# to find out from the first student.
if ! /opt/printvendo/venv/bin/printvendo-agent check; then
  echo "The kiosk is not ready. Fix the above, then run that command again." >&2
  exit 1
fi

echo
echo "Done. The kiosk is printing."
echo "  Check it:   /opt/printvendo/venv/bin/printvendo-agent check"
echo "  Watch it:   journalctl -u printvendo-agent -f"
