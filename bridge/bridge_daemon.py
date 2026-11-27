"""Feishu <-> agent bridge daemon, CF-relay variant (long-connection mode).

Differences from the hub variant (guide sections 4.1 / 12):
- Heartbeat carries CONNECTION state, not just process liveness:
  heartbeat.json includes ws_state ("connected" | "stale"),
  ws_conn_present, ws_disconnected_seconds, ws_last_event_at,
  ws_last_reconnected_at, ws_reconnect_count, poll_last_ok_at,
  poll_last_error. Watchdogs judge by connection, never by pid.
- API fallback polling: every fallback_poll_interval_seconds (90s) the
  daemon pulls known chats' last-15-minutes messages via the Feishu
  REST API, but ONLY while the WS link is down and only within
  fallback_poll_window_seconds (900s) of the disconnect. Recovered
  messages are marked source="poll". 3 consecutive poll failures make
  the daemon exit (code 4) so systemd/supervisor restarts it clean.
- WS-stale self-exit: if the link was established and then stays down
  for ws_stale_exit_seconds (300s), the daemon exits with code 3.
  Code 3 is a NORMAL restart request: supervisor resets backoff and
  the systemd unit lists it in SuccessExitStatus.
- De-dup: check-seen BEFORE enqueue, claim AFTER the inbox file is on
  disk (FileQueue.enqueue_inbox_once), shared by WS and poll paths.
- lark-oapi compat: _ws_connect_kwargs is WRAPPED (never replaced) so
  both 1.7.3 (0-arg) and 1.9.0 (1-arg connection_headers) work.
- All HTTP calls send a browser User-Agent (Cloudflare 1010 blocks
  the default Python UA on workers.dev; harmless on Feishu itself).

Startup contract:
- Data dir = $FEISHU_BRIDGE_HOME if set, else the repo root (parent of
  this bridge/ directory). Code always resolves relative to this file.
- Reads config.json (non-secret) from the data dir, and ONE JSON line
  with {"app_id", "app_secret"} from stdin (fed by supervisor.py).
  The daemon never reads a credential file and never logs secrets.
- This daemon ONLY reads/writes the local file queue. All relay sync
  with the Cloudflare Worker is relay-sync.py's job (one path only;
  two paths would double-send replies).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("FEISHU_BRIDGE_HOME") or CODE_DIR).resolve()
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "logs" / "daemon.log"
HEARTBEAT_PATH = BASE_DIR / "state" / "heartbeat.json"
KNOWN_CHATS_PATH = BASE_DIR / "state" / "known_chat_ids.json"

BASE = "https://open.feishu.cn"
TOKEN_URL = f"{BASE}/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = f"{BASE}/open-apis/im/v1/messages?receive_id_type=chat_id"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EXIT_WS_STALE = 3
EXIT_POLL_FAILED = 4


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _http_json(url: str, *, token: str | None = None, body: dict | None = None,
               method: str = "GET", timeout: int = 30) -> dict:
    headers = {"User-Agent": BROWSER_UA}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": -1, "msg": f"http {e.code}"}


class TokenCache:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - 300:
                return self._token
            data = _http_json(
                TOKEN_URL,
                body={"app_id": self._app_id, "app_secret": self._app_secret},
                method="POST",
                timeout=20,
            )
            if data.get("code") != 0:
                raise RuntimeError(
                    f"token exchange failed code={data.get('code')} msg={data.get('msg')}"
                )
            self._token = data["tenant_access_token"]
            self._expires_at = now + int(data.get("expire") or 7200)
            return self._token


# ---------------------------------------------------------------------------
# Sending (text + attachments)
# ---------------------------------------------------------------------------

def _multipart(fields: dict, file_field: str, filename: str, data: bytes,
               content_type: str) -> tuple[bytes, str]:
    import uuid

    boundary = "----feishu-bridge-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(data)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _post_multipart(url: str, token: str, fields: dict, file_field: str,
                    path: Path, content_type: str) -> dict:
    data = path.read_bytes()
    body, boundary = _multipart(fields, file_field, path.name, data, content_type)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
            "User-Agent": BROWSER_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": -1, "msg": f"http {e.code}"}


def upload_image(token_cache: TokenCache, path: Path) -> dict:
    return _post_multipart(
        f"{BASE}/open-apis/im/v1/images",
        token_cache.get(),
        {"image_type": "message"},
        "image",
        path,
        "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
    )


def upload_file(token_cache: TokenCache, path: Path) -> dict:
    suffix = path.suffix.lower().lstrip(".")
    file_type_map = {"mp4": "mp4", "pdf": "pdf", "xls": "xls", "xlsx": "xls",
                     "doc": "doc", "docx": "doc", "opus": "opus"}
    return _post_multipart(
        f"{BASE}/open-apis/im/v1/files",
        token_cache.get(),
        {"file_type": file_type_map.get(suffix, "stream"), "file_name": path.name},
        "file",
        path,
        "application/octet-stream",
    )


def send_message(token_cache: TokenCache, chat_id: str, msg_type: str,
                 content_obj: dict) -> dict:
    return _http_json(
        SEND_URL,
        token=token_cache.get(),
        body={"receive_id": chat_id, "msg_type": msg_type,
              "content": json.dumps(content_obj, ensure_ascii=False)},
        method="POST",
        timeout=20,
    )


def send_text(token_cache: TokenCache, chat_id: str, text: str) -> dict:
    return send_message(token_cache, chat_id, "text", {"text": text})


def send_attachment(token_cache: TokenCache, chat_id: str, kind: str,
                    file_path: str) -> dict:
    """Upload one local file and send it. kind: 'file' | 'image' | 'video'."""
    path = Path(file_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.is_file():
        return {"code": -2, "msg": "attachment file not found", "stage": "local"}
    try:
        if kind == "image":
            up = upload_image(token_cache, path)
            key = ((up.get("data") or {}).get("image_key")) if isinstance(up, dict) else None
            if up.get("code") != 0 or not key:
                return {"code": up.get("code"), "msg": up.get("msg"), "stage": "upload"}
            return {**send_message(token_cache, chat_id, "image", {"image_key": key}),
                    "stage": "send"}
        up = upload_file(token_cache, path)
        key = ((up.get("data") or {}).get("file_key")) if isinstance(up, dict) else None
        if up.get("code") != 0 or not key:
            return {"code": up.get("code"), "msg": up.get("msg"), "stage": "upload"}
        # Videos uploaded with file_type=mp4 must go out as a "media"
        # message, not a "file" message (Feishu error 230055 otherwise).
        if kind in ("video", "media") or path.suffix.lower() == ".mp4":
            return {**send_message(token_cache, chat_id, "media", {"file_key": key}),
                    "stage": "send"}
        return {**send_message(token_cache, chat_id, "file", {"file_key": key}),
                "stage": "send"}
    except Exception as exc:
        return {"code": -3, "msg": type(exc).__name__, "stage": "exception"}


# ---------------------------------------------------------------------------
# Incoming content parsing + attachment download
# ---------------------------------------------------------------------------

def parse_incoming_content(message_type: str, content: dict):
    """Return (text, resources, unsupported_reason).

    resources: [{"kind": "image"|"file", "file_key": str, "file_name": str}]
    """
    resources: list[dict] = []
    if message_type == "text":
        return str(content.get("text") or "").strip(), resources, None
    if message_type == "image":
        key = content.get("image_key")
        if key:
            resources.append({"kind": "image", "file_key": key, "file_name": f"{key}.jpg"})
        return "", resources, None
    if message_type in ("file", "media", "audio"):
        key = content.get("file_key")
        if key:
            name = str(content.get("file_name") or key)
            resources.append({"kind": "file", "file_key": key, "file_name": name})
        return "", resources, None
    if message_type == "post":
        texts: list[str] = []
        for para in (content.get("content") or []):
            for node in para or []:
                if isinstance(node, dict) and node.get("tag") == "text":
                    texts.append(str(node.get("text") or ""))
                elif isinstance(node, dict) and node.get("tag") == "img":
                    k = node.get("image_key")
                    if k:
                        resources.append({"kind": "image", "file_key": k,
                                          "file_name": f"{k}.jpg"})
            texts.append("\n")
        return "".join(texts).strip(), resources, None
    return "", resources, f"unsupported message_type={message_type}"


def download_incoming_attachments(token_cache: TokenCache, message_id: str,
                                  resources: list[dict]):
    """Download message resources into queue/incoming_attachments/<id>/."""
    attachments: list[dict] = []
    errors: list[dict] = []
    if not resources:
        return attachments, errors
    dest_dir = BASE_DIR / "queue" / "incoming_attachments" / message_id
    for res in resources:
        kind = res.get("kind") or "file"
        file_key = res.get("file_key") or ""
        file_name = Path(str(res.get("file_name") or file_key)).name
        url = (
            f"{BASE}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            f"?type={'image' if kind == 'image' else 'file'}"
        )
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token_cache.get()}",
                     "User-Agent": BROWSER_UA},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file_name
            dest.write_bytes(data)
            attachments.append({
                "type": "image" if kind == "image" else "file",
                "path": str(dest.relative_to(BASE_DIR)),
                "file_name": file_name,
                "size_bytes": len(data),
            })
        except Exception as exc:
            errors.append({"file_key": file_key, "error": type(exc).__name__})
    return attachments, errors


# ---------------------------------------------------------------------------
# WS connection state + heartbeat + stale-exit monitor
# ---------------------------------------------------------------------------

_started_at = time.time()
_ws_state = {
    "state": "starting",  # starting | connecting | connected | reconnecting
    "since": _started_at,
    "last_connected_ts": None,
    "last_event_ts": None,
    "last_reconnected_ts": None,
    "reconnect_count": 0,
    "ever_connected": False,
}
_ws_client_ref = {"client": None}
_poll_state = {"last_ok_ts": None, "last_error": None, "consecutive_failures": 0}


def _ws_set(state: str) -> None:
    now = time.time()
    if _ws_state["state"] != state:
        _ws_state["state"] = state
        _ws_state["since"] = now
    if state == "connected":
        _ws_state["last_connected_ts"] = now
        _ws_state["ever_connected"] = True


def _ws_probe_connected() -> bool:
    """Best-effort live check of the SDK connection object.

    The SDK has no on-connected callback for the first connect, so the
    heartbeat thread probes the current client: a live websockets
    connection has close_code None.
    """
    client = _ws_client_ref.get("client")
    conn = getattr(client, "_conn", None) if client is not None else None
    if conn is None:
        return False
    try:
        return getattr(conn, "close_code", 0) is None
    except Exception:
        return False


def _ws_connected() -> bool:
    return _ws_state["state"] == "connected" or _ws_probe_connected()


def _ws_disconnected_seconds() -> int:
    if _ws_connected():
        return 0
    last = _ws_state["last_connected_ts"] or _started_at
    return int(time.time() - last)


def heartbeat_loop(stop: threading.Event) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        try:
            if _ws_state["state"] != "connected" and _ws_probe_connected():
                _ws_set("connected")
            connected = _ws_connected()
            payload = {
                "ts": int(time.time()),
                "pid": os.getpid(),
                "started_at": int(_started_at),
                "uptime_seconds": int(time.time() - _started_at),
                # Connection state first: watchdogs judge by these.
                "ws_state": "connected" if connected else "stale",
                "ws_conn_present": bool(connected),
                "ws_disconnected_seconds": _ws_disconnected_seconds(),
                "ws_sdk_state": _ws_state["state"],
                "ws_state_since": int(_ws_state["since"]),
                "ws_last_event_at": (
                    int(_ws_state["last_event_ts"]) if _ws_state["last_event_ts"] else None
                ),
                "ws_last_connected_ts": (
                    int(_ws_state["last_connected_ts"])
                    if _ws_state["last_connected_ts"] else None
                ),
                "ws_last_reconnected_at": (
                    int(_ws_state["last_reconnected_ts"])
                    if _ws_state["last_reconnected_ts"] else None
                ),
                "ws_reconnect_count": _ws_state["reconnect_count"],
                "poll_last_ok_at": (
                    int(_poll_state["last_ok_ts"]) if _poll_state["last_ok_ts"] else None
                ),
                "poll_last_error": _poll_state["last_error"],
            }
            tmp = HEARTBEAT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(HEARTBEAT_PATH)
        except Exception:
            pass
        stop.wait(15.0)


def stale_monitor_loop(config: dict, stop: threading.Event) -> None:
    """Exit code 3 when a once-established WS link stays down too long.

    Quiet periods never trigger this: it only fires after a real
    connection existed and has been continuously down for
    ws_stale_exit_seconds (default 300).
    """
    stale_seconds = int(config.get("ws_stale_exit_seconds") or 300)
    while not stop.wait(10.0):
        if _ws_state["ever_connected"] and not _ws_connected():
            down = _ws_disconnected_seconds()
            if down > stale_seconds:
                log(f"ws stale exit: disconnected_seconds={down} limit={stale_seconds} "
                    f"exit_code={EXIT_WS_STALE}")
                os._exit(EXIT_WS_STALE)


# ---------------------------------------------------------------------------
# Shared incoming-message path (WS events AND API fallback poll)
# ---------------------------------------------------------------------------

def _remember_chat(chat_id: str) -> None:
    if not chat_id:
        return
    try:
        known = []
        if KNOWN_CHATS_PATH.exists():
            known = json.loads(KNOWN_CHATS_PATH.read_text(encoding="utf-8")) or []
        if chat_id not in known:
            known.append(chat_id)
            KNOWN_CHATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = KNOWN_CHATS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(known), encoding="utf-8")
            tmp.replace(KNOWN_CHATS_PATH)
    except Exception:
        pass


def _known_chat_ids(config: dict) -> list[str]:
    chats = list(config.get("allowed_chat_ids") or [])
    try:
        if KNOWN_CHATS_PATH.exists():
            for c in json.loads(KNOWN_CHATS_PATH.read_text(encoding="utf-8")) or []:
                if c not in chats:
                    chats.append(c)
    except Exception:
        pass
    return chats


def process_incoming_message(queue, config: dict, token_cache: TokenCache, *,
                             message_id: str, chat_id: str, chat_type: str,
                             sender_open_id: str, message_type: str,
                             content: dict, create_time: str,
                             source: str) -> bool:
    """Whitelist -> parse -> download attachments -> dedupe-enqueue.

    Returns True if enqueued. De-dup is check-seen BEFORE enqueue and
    claim AFTER the file is on disk (enqueue_inbox_once).
    """
    if not message_id or not chat_id:
        return False
    try:
        fresh = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        allowed = set(fresh.get("allowed_open_ids") or [])
        require_mention = bool(fresh.get("require_mention_in_group", True))
    except Exception:
        allowed = set(config.get("allowed_open_ids") or [])
        require_mention = bool(config.get("require_mention_in_group", True))
    if allowed and sender_open_id not in allowed:
        log(f"inbox ignored non-whitelisted sender message_id={message_id} source={source}")
        return False
    if queue.seen(message_id):
        log(f"inbox duplicate message_id={message_id} source={source}")
        return False
    text, resources, unsupported = parse_incoming_content(message_type, content)
    if chat_type == "group" and require_mention:
        # Feishu renders mentions in text content as @_user_<n>; a group
        # message without one was not addressed to the bot.
        if "@_user" not in str(content.get("text") or ""):
            log(f"inbox ignored group message without mention message_id={message_id} "
                f"source={source}")
            return False
    if not text and not resources:
        log(f"inbox ignored empty/non-text message_id={message_id} source={source} "
            f"reason={unsupported}")
        return False
    attachments, attachment_errors = download_incoming_attachments(
        token_cache, message_id, resources
    )
    payload = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_open_id": sender_open_id,
        "message_type": message_type,
        "text": text,
        "attachments": attachments,
        "attachment_errors": attachment_errors,
        "unsupported_reason": unsupported,
        "create_time": create_time,
        "source": source,
    }
    if not queue.enqueue_inbox_once(payload):
        log(f"inbox duplicate message_id={message_id} source={source}")
        return False
    _remember_chat(chat_id)
    try:
        (BASE_DIR / "state" / "last_sender.json").write_text(
            json.dumps({"sender_open_id": sender_open_id, "chat_id": chat_id},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    log(f"inbox enqueued message_id={message_id} source={source} "
        f"attachments={len(attachments)} attachment_errors={len(attachment_errors)}")
    return True


# ---------------------------------------------------------------------------
# API fallback polling (only while WS is down, only within 15 min of drop)
# ---------------------------------------------------------------------------

def _poll_once(queue, config: dict, token_cache: TokenCache) -> bool:
    window_s = int(config.get("fallback_poll_window_seconds") or 900)
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - window_s * 1000
    app_id = str(config.get("app_id") or "")
    ok = True
    for chat_id in _known_chat_ids(config):
        page_token = None
        for _ in range(20):  # page cap
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": "50",
                "sort_type": "ByCreateTimeDesc",
            }
            if page_token:
                params["page_token"] = page_token
            url = f"{BASE}/open-apis/im/v1/messages?{urllib.parse.urlencode(params)}"
            data = _http_json(url, token=token_cache.get(), timeout=30)
            if data.get("code") != 0:
                log(f"poll failed chat_id_present=True code={data.get('code')} "
                    f"msg={data.get('msg')}")
                if data.get("code") == 99991672:
                    log("poll blocked: app lacks im:message:readonly; add it in the "
                        "Feishu developer console and publish a new version")
                ok = False
                break
            items = ((data.get("data") or {}).get("items") or [])
            stop_paging = False
            for item in items:
                try:
                    create_ms = int(item.get("create_time") or 0)
                except Exception:
                    create_ms = 0
                if create_ms and create_ms < cutoff_ms:
                    stop_paging = True
                    break
                sender = item.get("sender") or {}
                sender_id = str(sender.get("id") or "")
                if sender.get("sender_type") == "app" or (app_id and sender_id == app_id):
                    continue  # never ingest the bot's own messages
                content_raw = ((item.get("body") or {}).get("content")) or "{}"
                try:
                    content = json.loads(content_raw) if isinstance(content_raw, str) else {}
                except Exception:
                    content = {}
                process_incoming_message(
                    queue, config, token_cache,
                    message_id=str(item.get("message_id") or ""),
                    chat_id=str(item.get("chat_id") or chat_id),
                    chat_type="",
                    sender_open_id=sender_id,
                    message_type=str(item.get("msg_type") or ""),
                    content=content if isinstance(content, dict) else {},
                    create_time=str(item.get("create_time") or ""),
                    source="poll",
                )
            if stop_paging or not ((data.get("data") or {}).get("has_more")):
                break
            page_token = ((data.get("data") or {}).get("page_token")) or None
            if not page_token:
                break
    return ok


def poll_loop(queue, config: dict, token_cache: TokenCache,
              stop: threading.Event) -> None:
    interval = float(config.get("fallback_poll_interval_seconds") or 90)
    window_s = int(config.get("fallback_poll_window_seconds") or 900)
    while not stop.wait(interval):
        if _ws_connected():
            _poll_state["consecutive_failures"] = 0
            continue
        # Active only within the window right after a disconnect.
        if _ws_disconnected_seconds() > window_s:
            continue
        try:
            if _poll_once(queue, config, token_cache):
                _poll_state["last_ok_ts"] = time.time()
                _poll_state["last_error"] = None
                _poll_state["consecutive_failures"] = 0
            else:
                raise RuntimeError("poll api error")
        except Exception as exc:
            _poll_state["last_error"] = type(exc).__name__
            _poll_state["consecutive_failures"] += 1
            log(f"poll round failed count={_poll_state['consecutive_failures']} "
                f"error={type(exc).__name__}")
            if _poll_state["consecutive_failures"] >= 3:
                log(f"poll failed 3x consecutively; exiting exit_code={EXIT_POLL_FAILED}")
                os._exit(EXIT_POLL_FAILED)


# ---------------------------------------------------------------------------
# Outbox watcher (agent replies -> Feishu)
# ---------------------------------------------------------------------------

def outbox_loop(config: dict, token_cache: TokenCache, stop: threading.Event) -> None:
    queue_dir = BASE_DIR / (config.get("queue_dir") or "queue")
    outbox = queue_dir / "outbox"
    processed = queue_dir / "processed"
    outbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    log("outbox watcher started")
    while not stop.is_set():
        try:
            for path in sorted(outbox.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    chat_id = payload.get("chat_id") or ""
                    text = payload.get("text") or ""
                    attachments = list(payload.get("attachments") or [])
                    if payload.get("image_path"):
                        attachments.append({"type": "image", "path": payload["image_path"]})
                    if payload.get("file_path"):
                        attachments.append({"type": "file", "path": payload["file_path"]})
                    if not chat_id or (not text and not attachments):
                        log(f"outbox skip incomplete file={path.name}")
                    else:
                        failures: list[dict] = []
                        if text:
                            resp = send_text(token_cache, chat_id, text)
                            if resp.get("code") == 0:
                                log(f"outbox sent text file={path.name}")
                            else:
                                failures.append({"code": resp.get("code"),
                                                 "msg": resp.get("msg"), "stage": "text"})
                                log(f"outbox text failed file={path.name} "
                                    f"code={resp.get('code')} msg={resp.get('msg')}")
                        for att in attachments:
                            if not isinstance(att, dict):
                                continue
                            kind = att.get("type") or "file"
                            resp = send_attachment(token_cache, chat_id, kind,
                                                   att.get("path") or "")
                            if resp.get("code") == 0:
                                log(f"outbox sent attachment kind={kind} file={path.name}")
                            else:
                                failures.append({"code": resp.get("code"),
                                                 "msg": resp.get("msg"),
                                                 "stage": resp.get("stage"), "kind": kind})
                                log(f"outbox attachment failed kind={kind} "
                                    f"stage={resp.get('stage')} code={resp.get('code')} "
                                    f"msg={resp.get('msg')} file={path.name}")
                        if failures:
                            (BASE_DIR / "state" / "last_send_error.json").write_text(
                                json.dumps(failures, ensure_ascii=False), encoding="utf-8"
                            )
                        else:
                            log(f"outbox sent file={path.name}")
                    path.replace(processed / path.name)
                except Exception as exc:
                    log(f"outbox error file={path.name} error={type(exc).__name__}")
        except Exception as exc:
            log(f"outbox loop error={type(exc).__name__}")
        stop.wait(2.0)


# ---------------------------------------------------------------------------
# lark-oapi _ws_connect_kwargs compat wrapper (wrap, never replace)
# ---------------------------------------------------------------------------

def _install_ws_proxy_wrapper() -> None:
    """Wrap the SDK's _ws_connect_kwargs to restore proxy discovery.

    lark-oapi 1.7.3: _ws_connect_kwargs() takes 0 args.
    lark-oapi 1.9.0: _ws_connect_kwargs(connection_headers=None) takes 1.
    The old approach (assigning a 0-arg lambda) breaks 1.9.0 with
    "takes 0 positional arguments but 1 was given". Wrapping the
    original with *args/**kwargs keeps both signatures working and
    preserves the connection headers the original computes.
    """
    try:
        import lark_oapi.ws.client as _ws_mod

        _orig = _ws_mod._ws_connect_kwargs

        def _wrapped(*args, **kwargs):
            result = _orig(*args, **kwargs)
            if not isinstance(result, dict):
                result = {}
            result = dict(result)
            result.setdefault("proxy", True)  # env proxy discovery
            return result

        _ws_mod._ws_connect_kwargs = _wrapped  # type: ignore[attr-defined]
        log("ws connect kwargs wrapped (proxy discovery on, original preserved)")
    except Exception as exc:
        log(f"ws connect kwargs wrap failed error={type(exc).__name__}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    config = load_config()
    if str(config.get("api_host") or "open.feishu.cn") not in ("open.feishu.cn",):
        log("startup note: api_host in config is not open.feishu.cn; this template "
            "targets China Feishu (open.feishu.cn)")
    line = sys.stdin.readline()
    if not line.strip():
        log("startup failed: no credentials on stdin")
        return 2
    try:
        creds = json.loads(line)
        app_id = str(creds["app_id"])
        app_secret = str(creds["app_secret"])
    except Exception:
        log("startup failed: invalid credential JSON on stdin")
        return 2
    if app_id != (config.get("app_id") or ""):
        log("startup note: stdin app_id differs from config app_id; using stdin app_id")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from file_queue import FileQueue  # local bridge/file_queue.py

    queue = FileQueue(BASE_DIR / (config.get("queue_dir") or "queue"))
    token_cache = TokenCache(app_id, app_secret)

    stop = threading.Event()
    threading.Thread(target=outbox_loop, args=(config, token_cache, stop),
                     daemon=True).start()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=poll_loop, args=(queue, config, token_cache, stop),
                     daemon=True).start()
    threading.Thread(target=stale_monitor_loop, args=(config, stop),
                     daemon=True).start()
    log("daemon started (outbox + heartbeat + fallback poll + stale monitor)")

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def on_message(data: P2ImMessageReceiveV1) -> None:
        _ws_state["last_event_ts"] = time.time()
        if _ws_state["state"] != "connected":
            _ws_set("connected")
        try:
            event = get_attr(data, "event")
            message = get_attr(event, "message")
            sender = get_attr(event, "sender")
            sender_id = get_attr(sender, "sender_id")
            open_id = get_attr(sender_id, "open_id") or ""
            content_raw = get_attr(message, "content") or "{}"
            try:
                content = json.loads(content_raw) if isinstance(content_raw, str) else {}
            except Exception:
                content = {}
            process_incoming_message(
                queue, config, token_cache,
                message_id=get_attr(message, "message_id") or "",
                chat_id=get_attr(message, "chat_id") or "",
                chat_type=get_attr(message, "chat_type") or "",
                sender_open_id=open_id,
                message_type=get_attr(message, "message_type") or "text",
                content=content if isinstance(content, dict) else {},
                create_time=str(get_attr(message, "create_time") or ""),
                source="ws",
            )
        except Exception as exc:
            log(f"on_message error={type(exc).__name__}")

    _install_ws_proxy_wrapper()

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    attempt = 0
    try:
        while not stop.is_set():
            attempt += 1
            run_started = time.time()
            log(f"starting Feishu WS long connection attempt={attempt}")
            ws_client = lark.ws.Client(
                app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO
            )
            _ws_client_ref["client"] = ws_client
            _ws_set("connecting")

            def _on_reconnecting() -> None:
                _ws_set("reconnecting")
                log("Feishu WS reconnecting; fallback poll covers the gap")

            def _on_reconnected() -> None:
                _ws_state["last_reconnected_ts"] = time.time()
                _ws_state["reconnect_count"] += 1
                _ws_set("connected")
                log("Feishu WS reconnected")

            ws_client.on_reconnecting = _on_reconnecting
            ws_client.on_reconnected = _on_reconnected
            try:
                ws_client.start()
                log("Feishu WS start returned unexpectedly; will reconnect")
            except Exception as exc:
                log(f"Feishu WS failed error={type(exc).__name__}; will reconnect")
            _ws_client_ref["client"] = None
            _ws_set("reconnecting")
            if stop.is_set():
                break
            if time.time() - run_started > 300:
                attempt = 0
            delay = min(60, 5 * max(1, attempt))
            log(f"ws reconnect in {delay}s")
            stop.wait(delay)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
