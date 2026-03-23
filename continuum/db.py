"""Single SQLite database for all Continuum state — tasks, results, checkpoints, handoffs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Task, TaskResult, TaskStatus, Checkpoint, Handoff

DEFAULT_DIR = Path.home() / ".continuum"
DEFAULT_DB  = DEFAULT_DIR / "continuum.db"


def get_db_path() -> Path:
    import os
    override = os.environ.get("CONTINUUM_DB")
    return Path(override) if override else DEFAULT_DB


class DB:
    """Unified store for tasks, results, checkpoints, and handoffs."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                priority   INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority  ON tasks(priority);

            CREATE TABLE IF NOT EXISTS results (
                task_id     TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id        TEXT PRIMARY KEY,
                project   TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cp_project
                ON checkpoints(project, timestamp DESC);

            CREATE TABLE IF NOT EXISTS handoffs (
                id           TEXT PRIMARY KEY,
                checkpoint_id TEXT NOT NULL,
                project      TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                data         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ho_project
                ON handoffs(project, generated_at DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS checkpoints_fts
            USING fts5(
                id,
                project,
                current_task,
                goal,
                context,
                findings_text,
                tokenize='porter ascii'
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def push_task(self, task: Task) -> Task:
        self._conn.execute(
            "INSERT INTO tasks (id, data, status, priority, created_at) VALUES (?,?,?,?,?)",
            (task.id, task.model_dump_json(), task.status.value,
             task.priority, task.created_at.isoformat())
        )
        self._conn.commit()
        return task

    def pop_next_task(self) -> Optional[Task]:
        """Atomically claim the next runnable pending task."""
        row = self._conn.execute("""
            SELECT data FROM tasks
            WHERE status = 'pending'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        task = Task.model_validate_json(row["data"])
        if task.depends_on:
            dep = self.get_task(task.depends_on)
            if dep and dep.status not in (TaskStatus.DONE,):
                return None
        from datetime import datetime, timezone
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        self._conn.execute(
            "UPDATE tasks SET status=?, data=? WHERE id=?",
            (task.status.value, task.model_dump_json(), task.id)
        )
        self._conn.commit()
        return task

    def update_task(self, task: Task) -> None:
        self._conn.execute(
            "UPDATE tasks SET status=?, data=? WHERE id=?",
            (task.status.value, task.model_dump_json(), task.id)
        )
        self._conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self._conn.execute(
            "SELECT data FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return Task.model_validate_json(row["data"]) if row else None

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> list[Task]:
        if status:
            rows = self._conn.execute(
                "SELECT data FROM tasks WHERE status=? ORDER BY priority ASC, created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM tasks ORDER BY priority ASC, created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [Task.model_validate_json(r["data"]) for r in rows]

    def cancel_task(self, task_id: str) -> bool:
        r = self._conn.execute(
            "UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('pending','waiting')",
            (task_id,)
        )
        self._conn.commit()
        return r.rowcount > 0

    def task_stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def save_result(self, result: TaskResult) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO results (task_id, data, finished_at) VALUES (?,?,?)",
            (result.task_id, result.model_dump_json(), result.finished_at.isoformat())
        )
        self._conn.commit()

    def get_result(self, task_id: str) -> Optional[TaskResult]:
        row = self._conn.execute(
            "SELECT data FROM results WHERE task_id=?", (task_id,)
        ).fetchone()
        return TaskResult.model_validate_json(row["data"]) if row else None

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def save_checkpoint(self, cp: Checkpoint) -> Checkpoint:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints(id, project, timestamp, data) VALUES (?,?,?,?)",
            (cp.id, cp.project, cp.timestamp.isoformat(), cp.model_dump_json())
        )
        # Keep FTS index in sync
        findings_text = " ".join(cp.findings + cp.dead_ends + cp.next_steps)
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints_fts(id, project, current_task, goal, context, findings_text)"
            " VALUES (?,?,?,?,?,?)",
            (cp.id, cp.project, cp.current_task, cp.goal, cp.context, findings_text)
        )
        self._conn.commit()
        return cp

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        row = self._conn.execute(
            "SELECT data FROM checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        return Checkpoint.model_validate_json(row["data"]) if row else None

    def latest_checkpoint(self, project: str) -> Optional[Checkpoint]:
        row = self._conn.execute(
            "SELECT data FROM checkpoints WHERE project=? ORDER BY timestamp DESC LIMIT 1",
            (project,)
        ).fetchone()
        return Checkpoint.model_validate_json(row["data"]) if row else None

    def list_checkpoints(self, project: Optional[str] = None, limit: int = 20) -> list[Checkpoint]:
        if project:
            rows = self._conn.execute(
                "SELECT data FROM checkpoints WHERE project=? ORDER BY timestamp DESC LIMIT ?",
                (project, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM checkpoints ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [Checkpoint.model_validate_json(r["data"]) for r in rows]

    def list_projects(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT project FROM checkpoints ORDER BY project"
        ).fetchall()
        return [r["project"] for r in rows]

    # ------------------------------------------------------------------
    # Handoffs
    # ------------------------------------------------------------------

    def save_handoff(self, h: Handoff) -> Handoff:
        self._conn.execute(
            "INSERT OR REPLACE INTO handoffs(id, checkpoint_id, project, generated_at, data) "
            "VALUES (?,?,?,?,?)",
            (h.id, h.checkpoint_id, h.project, h.generated_at.isoformat(), h.model_dump_json())
        )
        self._conn.commit()
        return h

    def latest_handoff(self, project: str) -> Optional[Handoff]:
        row = self._conn.execute(
            "SELECT data FROM handoffs WHERE project=? ORDER BY generated_at DESC LIMIT 1",
            (project,)
        ).fetchone()
        return Handoff.model_validate_json(row["data"]) if row else None

    # ------------------------------------------------------------------
    # Memory search (FTS + timeline + bulk get)
    # ------------------------------------------------------------------

    def search_checkpoints(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search over checkpoints. Returns compact summaries."""
        rows = self._conn.execute(
            """
            SELECT f.id, f.project, f.current_task, f.goal, c.timestamp
            FROM checkpoints_fts f
            JOIN checkpoints c ON c.id = f.id
            WHERE checkpoints_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "project": r["project"],
                "task": r["current_task"],
                "goal": r["goal"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def checkpoint_timeline(self, checkpoint_id: str, window: int = 5) -> list[Checkpoint]:
        """Return up to `window` checkpoints before and after the given one in the same project."""
        row = self._conn.execute(
            "SELECT project, timestamp FROM checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        if not row:
            return []
        project, ts = row["project"], row["timestamp"]

        before = self._conn.execute(
            "SELECT data FROM checkpoints WHERE project=? AND timestamp < ?"
            " ORDER BY timestamp DESC LIMIT ?",
            (project, ts, window),
        ).fetchall()
        after = self._conn.execute(
            "SELECT data FROM checkpoints WHERE project=? AND timestamp > ?"
            " ORDER BY timestamp ASC LIMIT ?",
            (project, ts, window),
        ).fetchall()
        pivot = self._conn.execute(
            "SELECT data FROM checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()

        ordered = list(reversed(before)) + ([pivot] if pivot else []) + after
        return [Checkpoint.model_validate_json(r["data"]) for r in ordered]

    def get_checkpoints_by_ids(self, ids: list[str]) -> list[Checkpoint]:
        """Fetch full checkpoint details for a list of IDs."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT data FROM checkpoints WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [Checkpoint.model_validate_json(r["data"]) for r in rows]

    def recent_project_summaries(self, days: int = 14) -> list[dict]:
        """Compact summary of all projects active in the last N days."""
        from datetime import timedelta, timezone
        from datetime import datetime as dt
        cutoff = (dt.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT project, MAX(timestamp) as latest, data
            FROM checkpoints
            WHERE timestamp >= ?
            GROUP BY project
            ORDER BY latest DESC
            """,
            (cutoff,),
        ).fetchall()
        summaries = []
        for r in rows:
            cp = Checkpoint.model_validate_json(r["data"])
            summaries.append({
                "project": cp.project,
                "status": cp.status,
                "task": cp.current_task,
                "goal": cp.goal,
                "next_step": cp.next_steps[0] if cp.next_steps else None,
                "dead_ends": len(cp.dead_ends),
                "last_updated": r["latest"],
                "checkpoint_id": cp.id,
            })
        return summaries

    def close(self) -> None:
        self._conn.close()
