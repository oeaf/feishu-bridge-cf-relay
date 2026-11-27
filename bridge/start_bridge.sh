#!/usr/bin/env bash
# Idempotent Feishu bridge starter (CF-relay variant).
# - Refuses to double-start: if a supervisor for THIS bridge's
#   credentials file is already running, do nothing. The pgrep pattern
#   includes the credentials path, which is unique per bridge, so
#   other bridges on the same host are never matched or touched.
# - Otherwise launch the supervisor detached; it reads credentials
#   from the local owner-only file and holds the secret in memory only.
#
# Usage: start_bridge.sh [BRIDGE_HOME] [CREDENTIALS_FILE]
#   BRIDGE_HOME       default: $FEISHU_BRIDGE_HOME or ~/workspace/feishu-bridge
#   CREDENTIALS_FILE  default: $BRIDGE_HOME/credentials.json
set -u
BASE="${1:-${FEISHU_BRIDGE_HOME:-$HOME/workspace/feishu-bridge}}"
CRED="${2:-$BASE/credentials.json}"
CODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$BASE/logs/supervisor_stdout.log"
mkdir -p "$BASE/logs" "$BASE/state" "$BASE/queue/inbox" "$BASE/queue/outbox" "$BASE/queue/processed"

if pgrep -f "supervisor.py --credentials-file $CRED" >/dev/null 2>&1; then
  echo "already_running"
  exit 0
fi

if [ ! -f "$CRED" ]; then
  echo "missing_credentials_file"
  exit 2
fi

PY="$BASE/.venv/bin/python"
[ -x "$PY" ] || PY="$CODE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

FEISHU_BRIDGE_HOME="$BASE" nohup setsid "$PY" -u "$CODE_DIR/bridge/supervisor.py" \
  --credentials-file "$CRED" >>"$LOG" 2>&1 &
echo "started pid=$!"
