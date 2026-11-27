#!/usr/bin/env bash
# Feishu bridge (CF-relay variant) end-of-install self-check.
# Run it on the side you just deployed; it prints a fixed-format
# report block the deploying agent pastes back verbatim.
#
#   selfcheck.sh [--slug <SLUG>] [--side server|sandbox]
#                [--home <BRIDGE_HOME>] [--relay-config <FILE>]
#                [--credentials <FILE>]
#
# Defaults: slug/home/relay settings come from the relay config when
# given; home defaults to ~/workspace/feishu-bridge (sandbox) or
# /var/lib/feishu-bridge/<SLUG> (server). Side auto-detects: if the
# systemd unit feishu-bridge-<SLUG>.service exists, "server".
# No secret values are ever printed (tokens/secrets stay in memory).
set -u

SLUG="${SLUG:-}"
SIDE="${SIDE:-auto}"
BRIDGE_HOME="${BRIDGE_HOME:-}"
RELAY_CONFIG="${RELAY_CONFIG:-}"
CREDENTIALS_FILE="${CREDENTIALS_FILE:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --slug) SLUG="$2"; shift 2 ;;
    --side) SIDE="$2"; shift 2 ;;
    --home) BRIDGE_HOME="$2"; shift 2 ;;
    --relay-config) RELAY_CONFIG="$2"; shift 2 ;;
    --credentials) CREDENTIALS_FILE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Pull slug/home from the relay config when not given explicitly.
if [ -n "$RELAY_CONFIG" ] && [ -f "$RELAY_CONFIG" ]; then
  eval "$(python3 - "$RELAY_CONFIG" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"CFG_SLUG={cfg.get('slug','')}")
print(f"CFG_HOME={Path(str(cfg.get('home_dir') or '')).expanduser()}")
PY
)"
  [ -z "$SLUG" ] && SLUG="${CFG_SLUG:-}"
  [ -z "$BRIDGE_HOME" ] && BRIDGE_HOME="${CFG_HOME:-}"
fi
[ -z "$SLUG" ] && SLUG="<SLUG>"

if [ "$SIDE" = "auto" ]; then
  if systemctl list-unit-files "feishu-bridge-$SLUG.service" >/dev/null 2>&1 \
     && systemctl list-unit-files "feishu-bridge-$SLUG.service" 2>/dev/null | grep -q "$SLUG"; then
    SIDE="server"
  else
    SIDE="sandbox"
  fi
fi
if [ -z "$BRIDGE_HOME" ]; then
  if [ "$SIDE" = "server" ]; then
    BRIDGE_HOME="/var/lib/feishu-bridge/$SLUG"
  else
    BRIDGE_HOME="$HOME/workspace/feishu-bridge"
  fi
fi
[ -z "$CREDENTIALS_FILE" ] && CREDENTIALS_FILE="$BRIDGE_HOME/credentials.json"

PASS=0; FAIL=0; SKIP=0
declare -a LINES
add() { # add <item> <PASS|FAIL|SKIP> <detail>
  LINES+=("$1: $2 $3")
  case "$2" in
    PASS) PASS=$((PASS+1)) ;;
    FAIL) FAIL=$((FAIL+1)) ;;
    SKIP) SKIP=$((SKIP+1)) ;;
  esac
}

# --- (a) bot identity via Feishu bot/v3/info -------------------------------
identity_out="$(python3 - "$CREDENTIALS_FILE" <<'PY'
import json, sys, urllib.request
from pathlib import Path
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
p = Path(sys.argv[1])
if not p.is_file():
    print("SKIP credentials file not present on this side")
    raise SystemExit
try:
    creds = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    print("FAIL credentials file unreadable")
    raise SystemExit
app_id = str(creds.get("app_id") or "")
secret = str(creds.get("app_secret") or "")
if not app_id or not secret or app_id == "cli_xxx" or "<" in app_id or "<" in secret:
    print("SKIP credentials are placeholders")
    raise SystemExit
def call(url, body=None, token=None):
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))
try:
    tok = call("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": app_id, "app_secret": secret})
    if tok.get("code") != 0:
        print(f"FAIL token exchange code={tok.get('code')}")
        raise SystemExit
    info = call("https://open.feishu.cn/open-apis/bot/v3/info",
                token=tok["tenant_access_token"])
    bot = (info.get("bot") or info.get("data", {}).get("bot") or {})
    name = bot.get("app_name") or bot.get("name") or "?"
    if info.get("code") in (0, None):
        print(f"PASS bot_name={name} app_id={app_id}")
    else:
        print(f"FAIL bot/v3/info code={info.get('code')}")
except SystemExit:
    raise
except Exception as exc:
    print(f"FAIL identity check error={type(exc).__name__}")
PY
)"
add "identity" "${identity_out%% *}" "${identity_out#* }"

# --- (b) processes ---------------------------------------------------------
if [ "$SIDE" = "server" ]; then
  proc_out="PASS"
  for unit in "feishu-bridge-$SLUG.service" "feishu-relay-server-$SLUG.service"; do
    state="$(systemctl is-active "$unit" 2>/dev/null || echo unknown)"
    if [ "$state" != "active" ]; then
      proc_out="FAIL $unit=$state"
      break
    fi
  done
  [ "$proc_out" = "PASS" ] && proc_out="PASS systemd units active"
  add "processes" "${proc_out%% *}" "${proc_out#* }"
else
  pidfile="$BRIDGE_HOME/state/relay-sync-sandbox.pid"
  if [ -f "$pidfile" ] && pid="$(cat "$pidfile" 2>/dev/null)" && [ -n "$pid" ] \
     && kill -0 "$pid" 2>/dev/null; then
    add "processes" "PASS" "relay-sync sandbox alive pid=$pid"
  else
    add "processes" "FAIL" "relay-sync sandbox process not running"
  fi
fi

# --- (c) relay reachable + auth, and fetch heartbeat for (d) ---------------
relay_out="$(python3 - "$RELAY_CONFIG" <<'PY'
import json, sys, urllib.error, urllib.request
from pathlib import Path
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
cfg_path = sys.argv[1]
if not cfg_path or not Path(cfg_path).is_file():
    print("SKIP no relay config given")
    print("HBJSON:")
    raise SystemExit
cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
url = str(cfg.get("relay_url") or "").rstrip("/")
slug = str(cfg.get("slug") or "")
token = str(cfg.get("relay_token") or "")
if not token and cfg.get("relay_token_file"):
    token = Path(str(cfg["relay_token_file"])).expanduser().read_text(encoding="utf-8").strip()
if not url or not slug or not token or "<" in url or "<" in token:
    print("SKIP relay config incomplete or placeholders")
    print("HBJSON:")
    raise SystemExit
req = urllib.request.Request(f"{url}/hb/{slug}",
                             headers={"Authorization": f"Bearer {token}",
                                      "User-Agent": UA}, method="GET")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
    print("PASS worker reachable, auth ok, heartbeat present")
    print(f"HBJSON:{body}")
except urllib.error.HTTPError as e:
    if e.code == 404:
        print("PASS worker reachable, auth ok (no heartbeat stored yet)")
    elif e.code in (401, 403):
        print(f"FAIL relay auth rejected http={e.code}")
    else:
        print(f"FAIL relay http={e.code}")
    print("HBJSON:")
except Exception as exc:
    print(f"FAIL relay unreachable error={type(exc).__name__}")
    print("HBJSON:")
PY
)"
relay_line="$(printf '%s\n' "$relay_out" | head -n 1)"
add "relay" "${relay_line%% *}" "${relay_line#* }"
hb_json="$(printf '%s\n' "$relay_out" | grep '^HBJSON:' | head -n 1 | cut -d: -f2-)"

# --- (d) heartbeat freshness + ws_state ------------------------------------
if [ "$SIDE" = "server" ] && [ -f "$BRIDGE_HOME/state/heartbeat.json" ]; then
  hb_json="$(cat "$BRIDGE_HOME/state/heartbeat.json")"
fi
hb_out="$(python3 - "$hb_json" <<'PY'
import json, sys, time
raw = sys.argv[1] if len(sys.argv) > 1 else ""
if not raw:
    print("SKIP no heartbeat available")
    raise SystemExit
try:
    hb = json.loads(raw)
except Exception:
    print("FAIL heartbeat unparsable")
    raise SystemExit
age = int(time.time()) - int(hb.get("ts") or 0)
state = hb.get("ws_state")
if age < 120 and state == "connected":
    print(f"PASS age={age}s ws_state=connected")
elif age >= 120:
    print(f"FAIL age={age}s ws_state={state}")
else:
    print(f"FAIL age={age}s ws_state={state} (not connected)")
PY
)"
add "heartbeat" "${hb_out%% *}" "${hb_out#* }"

# --- report ------------------------------------------------------------------
echo "=== FEISHU BRIDGE SELFCHECK ==="
echo "slug: $SLUG"
echo "side: $SIDE"
for line in "${LINES[@]}"; do echo "$line"; done
if [ "$FAIL" -eq 0 ]; then result="PASS"; else result="FAIL"; fi
echo "result: $result (pass=$PASS fail=$FAIL skip=$SKIP)"
echo "=== END ==="
[ "$FAIL" -eq 0 ]
