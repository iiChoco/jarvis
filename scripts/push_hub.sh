#!/bin/sh
# Push this checkout to the hub and let its autoreloader pick it up.
#
#     scripts/push_hub.sh              # sync the source tree
#     scripts/push_hub.sh --sync       # ...and re-sync the hub's dependencies
#
# The hub runs its own copy of the repo on the server (see [[deploy/
# ciel-hub.service]]); an edit here reaches it only when copied. Its
# SourceWatcher re-execs the process the moment the files land, so this
# is the whole deploy step. The host defaults to the tailnet name and
# can be overridden: CIEL_HUB_HOST=ciel@1.2.3.4 scripts/push_hub.sh.
set -e
HOST="${CIEL_HUB_HOST:-ciel@172.184.253.239}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rsync -az --delete \
  --exclude .venv --exclude .git --exclude __pycache__ \
  --exclude .claude --exclude reports \
  "$ROOT/" "$HOST:~/jarvis/"
echo "pushed to $HOST"
if [ "$1" = "--sync" ]; then
  ssh "$HOST" 'cd ~/jarvis && ~/.local/bin/uv sync --no-default-groups --group hub --extra discord --extra web 2>&1 | tail -2'
fi
