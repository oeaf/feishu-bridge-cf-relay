#!/usr/bin/env bash
# Server watchdog for ONE Feishu bridge (CF-relay variant).
# Judges CONNECTION liveness from the daemon heartbeat, not pid:
#   - heartbeat file missing/unreadable or stale (> 300 s), OR
#   - ws_state != connected with ws_disconnected_seconds > 300
# -> restart feishu-bridge-<SLUG>.service (at most once per 10 min).
# Pure bash + python3 stdlib; no tokens, no network, journal only.
#
# Install: replace <SLUG>, copy to /opt/feishu-bridge/feishu-watchdog-<SLUG>.sh
# (chmod 755), enable feishu-watchdog-<SLUG>.timer. Cron alternative:
#   * * * * * root /opt/feishu-bridge/feishu-watchdog-<SLUG>.sh
set -u
SLUG="<SLUG>"
HB="/var/lib/feishu-bridge/$SLUG/state/heartbeat.json"
STAMP="/var/lib/feishu-bridge/$SLUG/state/watchdog_last_restart"
SERVICE="feishu-bridge-$SLUG.service"

verdict="$(python3 - "$HB" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        hb = json.load(f)
except Exception:
    print("restart heartbeat_unreadable")
    raise SystemExit
now = int(time.time())
if now - int(hb.get("ts") or 0) > 300:
    print("restart heartbeat_stale")
    raise SystemExit
if hb.get("ws_state") != "connected":
    down = int(hb.get("ws_disconnected_seconds") or 0)
    if down > 300:
        print(f"restart ws_disconnected_{down}s")
        raise SystemExit
print("ok")
PY
)"

case "$verdict" in
  ok*) exit 0 ;;
  restart*)
    now="$(date +%s)"
    last=0
    [ -f "$STAMP" ] && last="$(cat "$STAMP" 2>/dev/null || echo 0)"
    if [ $((now - last)) -lt 600 ]; then
      logger -t "feishu-watchdog-$SLUG" "skip cooldown verdict=$verdict"
      exit 0
    fi
    echo "$now" > "$STAMP"
    logger -t "feishu-watchdog-$SLUG" "restart $SERVICE verdict=$verdict"
    systemctl restart "$SERVICE"
    ;;
  *) logger -t "feishu-watchdog-$SLUG" "unknown verdict=$verdict" ;;
esac
exit 0
