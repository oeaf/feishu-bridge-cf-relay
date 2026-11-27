#!/usr/bin/env bash
# Sandbox inbox hook (CF-relay variant): READ-ONLY.
# Checks ONLY the local inbox directory that sandbox relay-sync fills.
# No network calls, no writes, no secrets. New files -> wake with the
# file list; the worker reads the files itself. Empty -> silent.
# Register at a 30s poll interval.
#
# Inbox dir resolution: $FEISHU_BRIDGE_INBOX, else
# $FEISHU_BRIDGE_HOME/queue/inbox, else ~/workspace/feishu-bridge/queue/inbox.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

INBOX="${FEISHU_BRIDGE_INBOX:-${FEISHU_BRIDGE_HOME:-$HOME/workspace/feishu-bridge}/queue/inbox}"

if [[ ! -d "$INBOX" ]]; then
  log "inbox_missing" "{\"path\":\"queue/inbox\"}"
  silent "feishu inbox directory not present yet" "{}"
  exit 0
fi

# Build the wake payload (file names only, oldest first). Read-only:
# file presence is the signal; the worker moves each file to
# processed/ after handling it, so a failed run is retried next poll.
payload="$(python3 - "$INBOX" <<'PY'
import json, os, sys
inbox = sys.argv[1]
names = [n for n in os.listdir(inbox) if n.endswith(".json")]
names.sort(key=lambda n: os.path.getmtime(os.path.join(inbox, n)))
print(json.dumps({"files": names, "count": len(names)}, ensure_ascii=False))
PY
)"

count="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["count"])' "$payload")"
if [[ "$count" == "0" ]]; then
  silent "no new feishu messages" "{}"
  exit 0
fi

log "inbox_detected" "$payload"
wake "new Feishu message(s) waiting in local bridge inbox" "$payload"
