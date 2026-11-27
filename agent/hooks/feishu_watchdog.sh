#!/usr/bin/env bash
# Sandbox relay-sync watchdog hook (CF-relay variant), 60s poll.
# Zero-token while healthy (pure script):
#   - relay-sync process alive AND its local heartbeat fresh -> silent
#   - process dead -> restart it via start-relay-sync.sh, then wake
#     with the fixed payload {"reason":"relay_sync_restarted", ...}
#   - heartbeat stale > 10 min (process alive but not syncing) ->
#     wake with {"reason":"relay_heartbeat_stale", ...}
# The worker prompt for the wake cases lives in hooks/prompts.md.
#
# Paths: $FEISHU_BRIDGE_HOME (default ~/workspace/feishu-bridge),
# launcher: $FEISHU_BRIDGE_START_SYNC (default: ../../start-relay-sync.sh
# relative to the deployed copy of this script — set the env var if
# your layout differs), config: $FEISHU_RELAY_CONFIG.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BRIDGE_HOME="${FEISHU_BRIDGE_HOME:-$HOME/workspace/feishu-bridge}"
PIDFILE="$BRIDGE_HOME/state/relay-sync-sandbox.pid"
HBFILE="$BRIDGE_HOME/state/relay_heartbeat_sandbox.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SYNC="${FEISHU_BRIDGE_START_SYNC:-$SCRIPT_DIR/../../start-relay-sync.sh}"
CONFIG="${FEISHU_RELAY_CONFIG:-$HOME/workspace/feishu-bridge-server/relay.json}"

pid_alive=0
pid=""
if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || echo "")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    pid_alive=1
  fi
fi

if [[ "$pid_alive" == "0" ]]; then
  # Dead: restart it ourselves; only wake if the restart happened so
  # the agent can confirm it stays up (or report if it will not start).
  if [[ -x "$START_SYNC" ]]; then
    "$START_SYNC" "$CONFIG" >/dev/null 2>&1 || true
    sleep 2
    newpid=""
    [[ -f "$PIDFILE" ]] && newpid="$(cat "$PIDFILE" 2>/dev/null || echo "")"
    if [[ -n "$newpid" ]] && kill -0 "$newpid" 2>/dev/null; then
      payload="$(python3 -c 'import json,sys; print(json.dumps({"reason":"relay_sync_restarted","pid":int(sys.argv[1])}))' "$newpid")"
      log "relay_sync_restarted" "$payload"
      wake "feishu relay-sync was down and has been restarted" "$payload"
      exit 0
    fi
  fi
  payload='{"reason":"relay_sync_down_unrecoverable"}'
  log "relay_sync_down" "$payload"
  wake "feishu relay-sync is down and auto-restart failed" "$payload"
  exit 0
fi

# Alive: judge by its local heartbeat age.
hb_age="$(python3 - "$HBFILE" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        hb = json.load(f)
    print(int(time.time()) - int(hb.get("ts") or 0))
except Exception:
    print(999999)
PY
)"
if [[ "$hb_age" -gt 600 ]]; then
  payload="$(python3 -c 'import json,sys; print(json.dumps({"reason":"relay_heartbeat_stale","age_seconds":int(sys.argv[1])}))' "$hb_age")"
  log "relay_heartbeat_stale" "$payload"
  wake "feishu relay-sync heartbeat is stale" "$payload"
  exit 0
fi

silent "relay-sync healthy" "{}"
