#!/usr/bin/env bash
# Update the agent on this machine, in one command.
#
#   sudo bash update-pi.sh
#
# Four steps have to happen in order and all four are easy to get wrong:
#
#   - pull in the directory the agent was actually installed from, not a nested
#     copy left by an earlier tar extract;
#   - `pip install .` -- the trailing dot is the argument, and dropping it gives
#     "You must give at least one requirement to install", which says nothing
#     about what was meant;
#   - restart, because pip does not. The process holds the old code in memory
#     and keeps reporting the old version;
#   - check what is actually running, rather than what is on disk.
#
# This does all four and prints the version it ended on, so "did the update
# take" is answered by the command that did it.
set -euo pipefail

VENV=/opt/printvendo/venv
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: it installs into $VENV and restarts a service." >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/pip" ]]; then
  echo "No agent installed at $VENV." >&2
  echo "This updates an existing machine. For a new one, use install-pi.sh." >&2
  exit 1
fi

echo "==> Fetching"
# In this script's own directory, so it cannot update one clone while the venv
# was installed from another.
git -C "$HERE" pull --ff-only

echo "==> Installing $(git -C "$HERE" log --oneline -1)"
"$VENV/bin/pip" install --quiet --upgrade "$HERE"

echo "==> Restarting"
systemctl restart printvendo-agent

# A moment for it to come up, so `check` is not racing the process it is asking
# about.
sleep 2

RUNNING=$("$VENV/bin/python" -c 'from agent.__main__ import VERSION; print(VERSION)')
echo "==> Now running $RUNNING"

if ! "$VENV/bin/printvendo-agent" check; then
  echo >&2
  echo "The agent is updated but not ready. Fix the above, then run:" >&2
  echo "    $VENV/bin/printvendo-agent check" >&2
  exit 1
fi

echo
echo "Done. The console will show $RUNNING within a minute."
echo "  watch it:  journalctl -u printvendo-agent -f"
