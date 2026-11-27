#!/usr/bin/env python3
"""Feishu bridge relay sync (Cloudflare Worker <-> local file queue).

One script, four modes (--mode), plus two combined modes for systemd
and the sandbox launcher:

  server-push    server queue/inbox  -> Worker      (user messages)
  server-pull    Worker -> server queue/outbox      (agent replies)
  sandbox-pull   Worker -> sandbox queue/inbox      (user messages)
  sandbox-push   sandbox queue/outbox -> Worker     (agent replies)
  server         server-push + server-pull each round
  sandbox        sandbox-pull + sandbox-push each round

Delivery contract (guide sections 4.2 / 5.2):
- PUSH: a local file is marked synced ONLY after the Worker confirms
  the PUT: the file is renamed in place to "<name>.synced". A crash
  between PUT and rename just re-PUTs the same bytes next round
  (idempotent overwrite on the Worker side).
- PULL: the remote file is deleted (POST /del) ONLY after the local
  file is fully written and its size matches the listing. If the same
  file already exists locally (inbox/outbox or processed/), the
  remote copy is deleted without re-downloading, so a re-push after a
  crash cannot make the agent answer twice.
- server-push also uploads the daemon heartbeat file every round
  (PUT /hbfile/<slug>) so the sandbox side and selfcheck can judge
  the daemon by CONNECTION state.

Isolation: every remote key, pidfile, heartbeat and local directory is
scoped by the configured slug and home_dir. The script refuses to
start if its own pidfile already names a live process, and it never
touches another slug's keys (all Worker paths embed this slug).

Config (JSON file, --config):
  {
    "relay_url": "https://<worker-host>",
    "relay_token": "<RELAY_TOKEN>",          // or "relay_token_file": "<path>"
    "slug": "<SLUG>",
    "home_dir": "<bridge data dir>",          // queue/ and state/ live under it
    "queue_dir": "<optional override>"
  }

Stdlib only. All requests send a browser User-Agent: the default
Python UA is blocked by Cloudflare with error 1010 (guide 4.3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_BODY = 400 * 1024


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


class Relay:
    def __init__(self, base_url: str, token: str, slug: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.slug = slug

    def _request(self, method: str, path: str, data: bytes | None = None) -> bytes:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Authorization": f"Bearer {self.token}",
                     "User-Agent": BROWSER_UA},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def put_file(self, remote_name: str, data: bytes) -> None:
        self._request("PUT", f"/q/{self.slug}/{remote_name}", data)

    def get_file(self, remote_name: str) -> bytes:
        return self._request("GET", f"/q/{self.slug}/{remote_name}")

    def list_files(self) -> list[dict]:
        raw = self._request("GET", f"/list/{self.slug}")
        items = json.loads(raw.decode("utf-8"))
        return items if isinstance(items, list) else []

    def delete_file(self, remote_name: str) -> None:
        self._request("POST", f"/del/{self.slug}/{remote_name}", b"")

    def put_heartbeat(self, data: bytes) -> None:
        self._request("PUT", f"/hbfile/{self.slug}", data)


class Syncer:
    """kind: 'inbox' flows server->sandbox; 'outbox' flows sandbox->server."""

    def __init__(self, relay: Relay, home: Path, queue_dir: Path) -> None:
        self.relay = relay
        self.home = home
        self.queue = queue_dir
        self.inbox = queue_dir / "inbox"
        self.outbox = queue_dir / "outbox"
        self.processed = queue_dir / "processed"
        for d in (self.inbox, self.outbox, self.processed):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _remote_name(kind: str, local_name: str) -> str:
        return f"{kind}--{local_name}"

    # -- push ---------------------------------------------------------------
    def push(self, kind: str) -> None:
        local_dir = self.inbox if kind == "inbox" else self.outbox
        for path in sorted(local_dir.glob("*.json")):
            if not SAFE_FILE_RE.match(path.name) or ".." in path.name:
                log(f"push skipped unsafe file name={path.name}")
                continue
            data = path.read_bytes()
            if len(data) > MAX_BODY:
                log(f"push skipped oversized file={path.name} size={len(data)}")
                continue
            remote = self._remote_name(kind, path.name)
            self.relay.put_file(remote, data)
            # Mark synced ONLY after the PUT succeeded.
            path.replace(path.with_suffix(path.suffix + ".synced"))
            log(f"pushed {kind} file={path.name} size={len(data)}")

    # -- pull ---------------------------------------------------------------
    def pull(self, kind: str) -> None:
        local_dir = self.inbox if kind == "inbox" else self.outbox
        prefix = f"{kind}--"
        for item in self.relay.list_files():
            remote = str(item.get("key") or "")
            if not remote.startswith(prefix):
                continue
            local_name = remote[len(prefix):]
            if not SAFE_FILE_RE.match(local_name) or ".." in local_name:
                log(f"pull skipped unsafe remote name={remote}")
                continue
            target = local_dir / local_name
            if target.exists() or (self.processed / local_name).exists():
                # Already have it locally: ACK the remote copy away so a
                # crash-recovery re-push cannot duplicate the message.
                self.relay.delete_file(remote)
                log(f"pull deduped {kind} file={local_name} (already local)")
                continue
            data = self.relay.get_file(remote)
            expected = int(item.get("size") or 0)
            if expected and len(data) != expected:
                raise IOError(
                    f"size mismatch file={local_name} got={len(data)} want={expected}")
            md5 = hashlib.md5(data).hexdigest()  # integrity record for the log line
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            if tmp.stat().st_size != len(data):
                tmp.unlink(missing_ok=True)
                raise IOError(f"short write file={local_name}")
            tmp.replace(target)
            # Delete remote ONLY after the local file is fully on disk.
            self.relay.delete_file(remote)
            log(f"pulled {kind} file={local_name} size={len(data)} md5={md5[:8]}")

    # -- heartbeat ------------------------------------------------------------
    def push_heartbeat(self) -> None:
        hb = self.home / "state" / "heartbeat.json"
        if hb.is_file():
            self.relay.put_heartbeat(hb.read_bytes())


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    slug = str(cfg.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise SystemExit(f"config error: slug {slug!r} must match [a-z0-9-]+")
    if not cfg.get("relay_url"):
        raise SystemExit("config error: relay_url missing")
    token = str(cfg.get("relay_token") or "")
    if not token and cfg.get("relay_token_file"):
        token = Path(str(cfg["relay_token_file"])).read_text(encoding="utf-8").strip()
    if not token or token.startswith("<"):
        raise SystemExit("config error: relay token missing (relay_token or relay_token_file)")
    home = Path(str(cfg.get("home_dir") or ".")).expanduser()
    cfg["_home"] = home
    cfg["_queue"] = Path(str(cfg.get("queue_dir") or home / "queue")).expanduser()
    cfg["_token"] = token
    return cfg


def run_round(syncer: Syncer, mode: str) -> None:
    if mode in ("server-push", "server"):
        syncer.push("inbox")
        syncer.push_heartbeat()
    if mode in ("sandbox-push", "sandbox"):
        syncer.push("outbox")
    if mode in ("sandbox-pull", "sandbox"):
        syncer.pull("inbox")
    if mode in ("server-pull", "server"):
        syncer.pull("outbox")


def write_local_heartbeat(home: Path, mode: str, slug: str) -> None:
    try:
        state = home / "state"
        state.mkdir(parents=True, exist_ok=True)
        p = state / f"relay_heartbeat_{mode}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": int(time.time()), "mode": mode,
                                   "slug": slug, "pid": os.getpid()}),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def acquire_pidfile(home: Path, mode: str) -> Path:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    pidfile = state / f"relay-sync-{mode}.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip() or 0)
            if pid > 0:
                os.kill(pid, 0)
                raise SystemExit(
                    f"refusing to start: relay-sync mode={mode} already running pid={pid}")
        except ProcessLookupError:
            pass
        except ValueError:
            pass
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    return pidfile


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu bridge relay sync")
    parser.add_argument("--mode", required=True,
                        choices=["server-push", "sandbox-pull", "sandbox-push",
                                 "server-pull", "server", "sandbox"])
    parser.add_argument("--config", required=True, help="relay config JSON")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true", help="run one round and exit")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    relay = Relay(str(cfg["relay_url"]), cfg["_token"], str(cfg["slug"]))
    syncer = Syncer(relay, cfg["_home"], cfg["_queue"])
    pidfile = acquire_pidfile(cfg["_home"], args.mode)
    log(f"relay-sync started mode={args.mode} slug={cfg['slug']} interval={args.interval}")
    try:
        while True:
            started = time.time()
            try:
                run_round(syncer, args.mode)
                write_local_heartbeat(cfg["_home"], args.mode, str(cfg["slug"]))
            except urllib.error.HTTPError as e:
                log(f"sync round failed http={e.code} (will retry next round)")
            except Exception as exc:
                log(f"sync round failed error={type(exc).__name__} (will retry next round)")
            if args.once:
                break
            time.sleep(max(1.0, args.interval - (time.time() - started)))
    finally:
        try:
            if pidfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pidfile.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
