"""Task execution engine with automatic checkpoint integration."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

import psutil

from .models import Task, TaskResult, TaskStatus

LOG_DIR = Path.home() / ".continuum" / "logs"


def check_resources(min_free_ram_mb: int) -> tuple[bool, str]:
    mem = psutil.virtual_memory()
    free_mb = mem.available / (1024 * 1024)
    if free_mb < min_free_ram_mb:
        return False, f"only {free_mb:.0f}MB free, need {min_free_ram_mb}MB"
    return True, "ok"


def run_task(task: Task) -> TaskResult:
    """Execute a task synchronously. Returns full result."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{task.id}.log"

    env = {**os.environ, **task.env}
    cwd = task.cwd or str(Path.home())
    start = time.time()

    try:
        proc = subprocess.run(
            task.command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=task.timeout,
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        exit_code = -1
        stdout = ""
        stderr = f"Task timed out after {task.timeout}s"
    except Exception as e:
        exit_code = -1
        stdout = ""
        stderr = str(e)

    duration = time.time() - start

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"=== CONTINUUM TASK {task.id} ===\n")
        f.write(f"Name:    {task.name}\n")
        f.write(f"Command: {task.command}\n")
        f.write(f"Exit:    {exit_code}\n")
        f.write(f"Time:    {duration:.2f}s\n\n")
        f.write("--- STDOUT ---\n")
        f.write(stdout)
        f.write("\n--- STDERR ---\n")
        f.write(stderr)

    return TaskResult(
        task_id=task.id,
        exit_code=exit_code,
        stdout=stdout[-8000:],
        stderr=stderr[-2000:],
        duration_seconds=duration,
    )


def auto_checkpoint(task: Task, result: TaskResult) -> None:
    """
    If task.auto_checkpoint and task.project are set, write a structured
    checkpoint directly to the DB after task completion.
    """
    if not (task.auto_checkpoint and task.project):
        return
    try:
        from .db import DB
        from .models import Checkpoint
        db = DB()
        status = "complete" if result.success else "in-progress"
        summary_lines = result.stdout.splitlines()
        findings = [l.strip() for l in summary_lines if l.strip()][:10]
        cp = Checkpoint(
            project=task.project,
            current_task=task.name,
            goal=f"Run: {task.command[:120]}",
            status=status,
            context=f"Forge task {task.id} {'succeeded' if result.success else 'failed'} in {result.duration_seconds:.1f}s",
            findings=findings,
            next_steps=[f"Review output: ~/.continuum/logs/{task.id}.log"],
            task_id=task.id,
            tags=task.tags,
        )
        db.save_checkpoint(cp)
    except Exception:
        pass


def _fire_notify(db, task: Task, result: TaskResult) -> None:
    """Fire task-complete notifications.  Checks per-task config first,
    then falls back to per-project config, then environment variables."""
    try:
        from .notify import NotifyConfig, notify, format_task_complete
        import json

        config_json = db.pop_task_notify(task.id)
        if config_json is None and task.project:
            config_json = db.get_notify_config(task.project)

        if config_json:
            raw = json.loads(config_json)
            cfg = NotifyConfig(**raw)
        else:
            cfg = NotifyConfig.from_env()

        if not cfg.has_any_channel():
            return
        if cfg.errors_only and result.success:
            return

        summary_lines = [l for l in result.stdout.splitlines() if l.strip()]
        summary = "\n".join(summary_lines[:5]) or (result.stderr[:200] if result.stderr else "No output.")
        title, msg = format_task_complete(task.id, task.project or "?", summary, result.success)
        notify(title, msg, config=cfg, extra={"task_id": task.id, "exit_code": result.exit_code})
    except Exception:
        pass


class ContinuumRunner:
    """Background thread pool — pulls from queue, executes, auto-checkpoints."""

    def __init__(
        self,
        db,
        workers: int = 2,
        on_complete: Optional[Callable[[Task, TaskResult], None]] = None,
    ):
        self.db = db
        self.workers = workers
        self.on_complete = on_complete
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(
                target=self._worker, name=f"continuum-worker-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            task = self.db.pop_next_task()
            if task is None:
                time.sleep(2)
                continue

            ok, reason = check_resources(task.min_free_ram_mb)
            if not ok:
                task.status = TaskStatus.PENDING
                task.started_at = None
                self.db.update_task(task)
                time.sleep(10)
                continue

            result = run_task(task)

            task.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
            task.finished_at = datetime.now(timezone.utc)
            self.db.update_task(task)
            self.db.save_result(result)

            # Auto-checkpoint if requested
            auto_checkpoint(task, result)

            # Notifications — fire any per-task or per-project notify config
            _fire_notify(self.db, task, result)

            if self.on_complete:
                try:
                    self.on_complete(task, result)
                except Exception:
                    pass
