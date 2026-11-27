"""Feishu bridge supervisor (keepalive), CF-relay variant.

Credential source is either one JSON line on stdin, or
`--credentials-file <path>` reading a local JSON file with owner-only
permissions when the operator explicitly chooses full auto-recovery
(the server deployment in the guide uses --credentials-file).
Credentials are held in this process's memory, fed to each daemon
child over a pipe, and never logged.

Code/data split (guide 4.1): CODE_DIR is this file's repo root and
never changes; BASE_DIR is $FEISHU_BRIDGE_HOME (per-bridge data dir:
config, queue, state, logs) and defaults to CODE_DIR. Mixing the two
up makes the daemon unfindable on multi-bridge servers.

Child exit codes:
- 3 (WS-stale self-exit) is a NORMAL restart request: the supervisor
  restarts the child immediately with the minimum backoff and does
  not grow the failure backoff.
- Any other exit uses capped exponential backoff (3s -> 60s), reset
  after a child runs > 300s.

Logs are sanitized: only pids, exit codes, durations, restart counts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("FEISHU_BRIDGE_HOME") or CODE_DIR).resolve()
DAEMON = CODE_DIR / "bridge" / "bridge_daemon.py"
LOG_PATH = BASE_DIR / "logs" / "supervisor.log"
HEARTBEAT = BASE_DIR / "state" / "supervisor_heartbeat.json"
CHILD_STATE = BASE_DIR / "state" / "daemon_child.json"

EXIT_WS_STALE = 3

_stop = False


def _python() -> str:
    for cand in (BASE_DIR / ".venv" / "bin" / "python",
                 CODE_DIR / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _handle_signal(signum, frame):  # noqa: ANN001
    global _stop
    _stop = True


def main() -> int:
    creds: dict = {}
    if len(sys.argv) >= 3 and sys.argv[1] == "--credentials-file":
        try:
            creds = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        except Exception:
            log("supervisor startup failed: cannot read credentials file")
            return 2
    else:
        line = sys.stdin.readline()
        if not line.strip():
            log("supervisor startup failed: no credentials on stdin")
            return 2
        try:
            creds = json.loads(line)
        except Exception:
            log("supervisor startup failed: invalid credential JSON")
            return 2
        del line
    try:
        app_id = str(creds["app_id"])
        app_secret = str(creds["app_secret"])
    except Exception:
        log("supervisor startup failed: credentials missing fields")
        return 2
    cred_line = json.dumps({"app_id": app_id, "app_secret": app_secret})
    creds = {}

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    restart_count = 0
    backoff = 3
    log("supervisor started; secret held only in memory")

    while not _stop:
        started = time.time()
        write_json(
            HEARTBEAT,
            {"ts": int(started), "pid": os.getpid(),
             "restart_count": restart_count, "phase": "starting_child"},
        )
        env = dict(os.environ)
        env["FEISHU_BRIDGE_HOME"] = str(BASE_DIR)
        try:
            child = subprocess.Popen(
                [_python(), "-u", str(DAEMON)],
                stdin=subprocess.PIPE,
                cwd=str(CODE_DIR),
                env=env,
            )
        except Exception as exc:
            log(f"child spawn failed error={type(exc).__name__}")
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
            continue

        write_json(
            CHILD_STATE,
            {"pid": child.pid, "started_at": int(started),
             "restart_count": restart_count},
        )
        log(f"daemon child started pid={child.pid} restart_count={restart_count}")
        try:
            assert child.stdin is not None
            child.stdin.write(cred_line.encode("utf-8") + b"\n")
            child.stdin.close()
        except Exception as exc:
            log(f"child credential pipe failed error={type(exc).__name__}")

        while not _stop:
            rc = child.poll()
            if rc is not None:
                break
            write_json(
                HEARTBEAT,
                {
                    "ts": int(time.time()),
                    "pid": os.getpid(),
                    "child_pid": child.pid,
                    "restart_count": restart_count,
                    "phase": "child_running",
                    "child_uptime_seconds": int(time.time() - started),
                },
            )
            time.sleep(5)

        if _stop:
            try:
                child.terminate()
                child.wait(timeout=10)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
            log("supervisor stopping; child terminated")
            break

        runtime = time.time() - started
        rc = child.returncode
        restart_count += 1
        if rc == EXIT_WS_STALE:
            # Normal restart request from the daemon's stale monitor:
            # no failure backoff growth.
            log(f"daemon child exited code=3 (ws-stale self-exit; normal restart) "
                f"runtime_seconds={int(runtime)} restart_count={restart_count}")
            backoff = 3
        else:
            log(f"daemon child exited code={rc} runtime_seconds={int(runtime)} "
                f"restart_count={restart_count}")
            if runtime > 300:
                backoff = 3
        write_json(
            HEARTBEAT,
            {
                "ts": int(time.time()),
                "pid": os.getpid(),
                "restart_count": restart_count,
                "phase": "restarting",
                "backoff_seconds": backoff,
            },
        )
        end = time.time() + backoff
        while not _stop and time.time() < end:
            time.sleep(1)
        if rc != EXIT_WS_STALE:
            backoff = min(60, backoff * 2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
