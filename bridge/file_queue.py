"""Local file queue for the Feishu <-> agent bridge (CF-relay variant).

Messages are stored as one JSON file per message id, with persistent
message_id de-duplication in state/seen_message_ids.json.

De-dup contract (guide section 4.1, learned the hard way):
- BEFORE enqueueing, check whether the message_id was already seen.
- The claim (marking the id as seen) happens only AFTER the inbox file
  is safely on disk. A crash between receive and enqueue therefore
  loses nothing, and the 90s API fallback poller cannot re-enqueue the
  same message inside its 15-minute window.

No credentials are read or stored here.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FileQueue:
    def __init__(self, queue_dir: Path) -> None:
        self.inbox = queue_dir / "inbox"
        self.outbox = queue_dir / "outbox"
        self.processed = queue_dir / "processed"
        self.state_file = queue_dir.parent / "state" / "seen_message_ids.json"
        for d in (self.inbox, self.outbox, self.processed, self.state_file.parent):
            d.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()
        # claim/check/enqueue are called from the WS event thread and
        # the fallback-poll thread; serialize the whole sequence.
        self._lock = threading.Lock()
        if self.state_file.exists():
            try:
                self._seen = set(json.loads(self.state_file.read_text(encoding="utf-8")))
            except Exception:
                self._seen = set()

    # -- de-dup primitives -------------------------------------------------
    def seen(self, message_id: str) -> bool:
        """True if this message_id was already claimed (check BEFORE enqueue)."""
        if not message_id:
            return True
        with self._lock:
            return message_id in self._seen

    def mark_seen(self, message_id: str) -> None:
        """Record a message_id as seen (call AFTER its file is on disk)."""
        if not message_id:
            return
        with self._lock:
            if message_id not in self._seen:
                self._seen.add(message_id)
                _atomic_write_json(self.state_file, sorted(self._seen))

    def claim(self, message_id: str) -> bool:
        """Check-and-mark in one step. Returns True only the first time.

        Kept for callers (backfill tooling) that enqueue immediately
        after claiming. The daemon's receive path uses
        enqueue_inbox_once(), which follows check -> write -> claim.
        """
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
            _atomic_write_json(self.state_file, sorted(self._seen))
        return True

    # -- enqueue ------------------------------------------------------------
    def enqueue_inbox(self, payload: dict) -> Path:
        message_id = payload.get("message_id") or "unknown"
        path = self.inbox / f"{message_id}.json"
        _atomic_write_json(path, payload)
        return path

    def enqueue_inbox_once(self, payload: dict) -> bool:
        """Check-seen -> write file -> claim, atomically.

        Returns True if the message was enqueued, False if it was a
        duplicate that had already been seen.
        """
        message_id = str(payload.get("message_id") or "")
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen:
                return False
            path = self.inbox / f"{message_id}.json"
            _atomic_write_json(path, payload)
            self._seen.add(message_id)
            _atomic_write_json(self.state_file, sorted(self._seen))
        return True

    def enqueue_outbox(self, payload: dict) -> Path:
        reply_to = payload.get("reply_to_message_id") or payload.get("chat_id") or "reply"
        path = self.outbox / f"{reply_to}.json"
        _atomic_write_json(path, payload)
        return path
