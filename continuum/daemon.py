"""Continuum daemon — persistent background task runner."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

CONTINUUM_DIR = Path.home() / ".continuum"
PID_FILE = CONTINUUM_DIR / "daemon.pid"
LOG_FILE = CONTINUUM_DIR / "daemon.log"


def start_daemon(workers: int = 2) -> None:
    CONTINUUM_DIR.mkdir(parents=True, exist_ok=True)

    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Continuum daemon already running (pid {pid})")
            return
        except (ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))

    from .db import DB
    from .runner import ContinuumRunner
    from .observer import ObserverThread

    db = DB()
    runner = ContinuumRunner(db, workers=workers)
    runner.start()

    # Auto-observe: compress method from env, default rule-based
    observe_method = os.environ.get("CONTINUUM_OBSERVE_METHOD", "rule")
    observer = ObserverThread(db, compress_every=20, method=observe_method)
    observer.start()

    print(f"Continuum daemon started (pid {os.getpid()}, {workers} workers)")
    print(f"DB:   {db.db_path}")
    print(f"Logs: {CONTINUUM_DIR / 'logs'}")
    print(f"Observer: compression method={observe_method}")

    def _shutdown(sig, frame):
        print("\nShutting down continuum daemon...")
        runner.stop()
        observer.stop()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        _shutdown(None, None)


def stop_daemon() -> None:
    if not PID_FILE.exists():
        print("No daemon running.")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to pid {pid}")
        PID_FILE.unlink(missing_ok=True)
    except ProcessLookupError:
        print("Daemon not found (stale pid file removed)")
        PID_FILE.unlink(missing_ok=True)


def daemon_status() -> dict:
    if not PID_FILE.exists():
        return {"running": False}
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 0)
        return {"running": True, "pid": pid}
    except (ProcessLookupError, PermissionError):
        return {"running": False, "stale_pid": pid}
