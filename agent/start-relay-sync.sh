#!/usr/bin/env bash
# Sandbox-side relay-sync launcher (CF-relay variant).
# Starts ONE relay-sync process in combined "sandbox" mode
# (pull inbox + push outbox each round) under nohup.
# - Refuses to double-start: if the pidfile for THIS bridge's home
#   names a live process, it exits without touching anything.
# - The pidfile lives inside this bridge's own state/ dir, so other
#   bridges' sync processes are never matched or disturbed.
#
# Usage: start-relay-sync.sh [RELAY_CONFIG]
#   RELAY_CONFIG default: $FEISHU_RELAY_CONFIG or
#   ~/workspace/feishu-bridge-server/relay.json
# The relay-sync.py path is resolved relative to this script
# (../relay/relay-sync.py); override with $RELAY_SYNC_PY.
set -u
CONFIG="${1:-${FEISHU_RELAY_CONFIG:-$HOME/workspace/feishu-bridge-server/relay.json}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_SYNC_PY="${RELAY_SYNC_PY:-$SCRIPT_DIR/../relay/relay-sync.py}"

if [ ! -f "$CONFIG" ]; then
  echo "missing_relay_config: $CONFIG"
  exit 2
fi
if [ ! -f "$RELAY_SYNC_PY" ]; then
  echo "missing_relay_sync_py: $RELAY_SYNC_PY"
  exit 2
fi

HOME_DIR="$(python3 - "$CONFIG" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(Path(str(cfg.get("home_dir") or ".")).expanduser())
PY
)"
PIDFILE="$HOME_DIR/state/relay-sync-sandbox.pid"
LOG="$HOME_DIR/logs/relay-sync-sandbox.log"
mkdir -p "$HOME_DIR/logs" "$HOME_DIR/state"

if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || echo "")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "already_running pid=$pid"
    exit 0
  fi
fi

nohup python3 "$RELAY_SYNC_PY" --mode sandbox --config "$CONFIG" --interval 30 \
  >>"$LOG" 2>&1 &
echo "started pid=$! config=$CONFIG"
