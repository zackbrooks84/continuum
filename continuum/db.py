"""Single SQLite database for all Continuum state — tasks, results, checkpoints, handoffs, observations."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Optional
import os
import logging
import threading

from .models import Task, TaskResult, TaskStatus, Checkpoint, Handoff, ProjectConfig, PatternSuggestion, UserMemory, AgentMemory

DEFAULT_DIR = Path.home() / ".continuum"
DEFAULT_DB  = DEFAULT_DIR / "continuum.db"
logger = logging.getLogger(__name__)


class CheckpointDataIntegrityError(RuntimeError):
    """Raised when checkpoint rows contain missing or invalid JSON payloads."""


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
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._profile_push_task = bool(os.environ.get("CONTINUUM_PROFILE_FORGE_PUSH"))
        self._last_checkpoint_timing: Optional[dict[str, float]] = None
        self._last_push_task_timing: Optional[dict[str, float]] = None
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

            CREATE TABLE IF NOT EXISTS project_configs (
                project TEXT PRIMARY KEY,
                data    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notify_configs (
                project TEXT PRIMARY KEY,
                data    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_notify (
                task_id TEXT PRIMARY KEY,
                data    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observations (
                id         TEXT PRIMARY KEY,
                project    TEXT NOT NULL,
                tool_name  TEXT NOT NULL,
                summary    TEXT NOT NULL,
                timestamp  TEXT NOT NULL,
                compressed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_obs_project_comp
                ON observations(project, compressed, timestamp);

            CREATE TABLE IF NOT EXISTS patterns (
                id          TEXT PRIMARY KEY,
                project     TEXT NOT NULL,
                pattern_key TEXT NOT NULL,
                frequency   INTEGER NOT NULL DEFAULT 1,
                data        TEXT NOT NULL,
                last_seen   TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_project_key
                ON patterns(project, pattern_key);

            CREATE TABLE IF NOT EXISTS token_events (
                id         TEXT PRIMARY KEY,
                project    TEXT,
                used       INTEGER NOT NULL,
                limit_     INTEGER NOT NULL,
                pct        REAL NOT NULL,
                timestamp  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_token_project
                ON token_events(project, timestamp DESC);
        """)
        # --- Personal Memory ---
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'cli',
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_um_key ON user_memory(key)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_um_category ON user_memory(category)"
        )
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS user_memory_fts
            USING fts5(id, key, value, category, tags, tokenize='porter ascii')
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'cli',
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_am_key ON agent_memory(key)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_am_category ON agent_memory(category)"
        )
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts
            USING fts5(id, key, value, category, rationale, tokenize='porter ascii')
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def push_task(self, task: Task) -> Task:
        if not self._profile_push_task:
            self._conn.execute(
                "INSERT INTO tasks (id, data, status, priority, created_at) VALUES (?,?,?,?,?)",
                (task.id, task.model_dump_json(), task.status.value,
                 task.priority, task.created_at.isoformat())
            )
            self._conn.commit()
            return task

        serialize_start = perf_counter()
        task_json = task.model_dump_json()
        created_at = task.created_at.isoformat()
        serialize_elapsed = perf_counter() - serialize_start

        sql_start = perf_counter()
        self._conn.execute(
            "INSERT INTO tasks (id, data, status, priority, created_at) VALUES (?,?,?,?,?)",
            (task.id, task_json, task.status.value, task.priority, created_at)
        )
        sql_elapsed = perf_counter() - sql_start

        commit_start = perf_counter()
        self._conn.commit()
        commit_elapsed = perf_counter() - commit_start

        self._last_push_task_timing = {
            "serialization_s": serialize_elapsed,
            "lock_wait_s": 0.0,
            "sql_execute_s": sql_elapsed,
            "commit_s": commit_elapsed,
        }
        return task

    def latest_push_task_timing(self) -> Optional[dict[str, float]]:
        return self._last_push_task_timing

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
        serialize_start = perf_counter()
        cp_json = cp.model_dump_json()
        if not cp_json:
            raise CheckpointDataIntegrityError(
                f"Checkpoint {cp.id} for project {cp.project} serialized to empty JSON payload."
            )
        findings_text = " ".join(cp.findings + cp.dead_ends + cp.next_steps)
        serialize_elapsed = perf_counter() - serialize_start

        sql_start = perf_counter()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints(id, project, timestamp, data) VALUES (?,?,?,?)",
                (cp.id, cp.project, cp.timestamp.isoformat(), cp_json)
            )
            # Keep FTS index in sync
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints_fts(id, project, current_task, goal, context, findings_text)"
                " VALUES (?,?,?,?,?,?)",
                (cp.id, cp.project, cp.current_task, cp.goal, cp.context, findings_text)
            )
            commit_start = perf_counter()
            self._conn.commit()
        sql_elapsed = perf_counter() - sql_start

        commit_elapsed = perf_counter() - commit_start
        self._last_checkpoint_timing = {
            "serialization_s": serialize_elapsed,
            "sql_execute_s": sql_elapsed,
            "commit_s": commit_elapsed,
        }
        return cp

    def latest_checkpoint_timing(self) -> Optional[dict[str, float]]:
        return self._last_checkpoint_timing

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

    def list_checkpoints(
        self,
        project: Optional[str] = None,
        limit: int = 20,
        strict: Optional[bool] = None,
    ) -> list[Checkpoint]:
        if strict is None:
            strict = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if project:
            rows = self._conn.execute(
                "SELECT id, project, timestamp, data FROM checkpoints WHERE project=? ORDER BY timestamp DESC LIMIT ?",
                (project, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, project, timestamp, data FROM checkpoints ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
        checkpoints: list[Checkpoint] = []
        invalid_rows: list[str] = []
        for r in rows:
            cp_id = r["id"] or "<missing-id>"
            cp_project = r["project"] or "<missing-project>"
            cp_timestamp = r["timestamp"] or "<missing-timestamp>"
            raw = r["data"]
            if raw is None:
                invalid_rows.append(
                    f"id={cp_id} project={cp_project} timestamp={cp_timestamp} reason=data is NULL"
                )
                continue
            try:
                checkpoints.append(Checkpoint.model_validate_json(raw))
            except Exception as exc:
                invalid_rows.append(
                    f"id={cp_id} project={cp_project} timestamp={cp_timestamp} reason={exc}"
                )

        if invalid_rows:
            msg = "Invalid checkpoint rows detected:\n" + "\n".join(invalid_rows)
            if strict:
                raise CheckpointDataIntegrityError(msg)
            logger.error(msg)
        return checkpoints

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

    def search_checkpoints(
        self,
        query: str,
        limit: int = 10,
        tags: Optional[list[str]] = None,
        exclude_tags: Optional[list[str]] = None,
    ) -> list[dict]:
        """Full-text search over checkpoints. Returns compact summaries.

        Pass tags/exclude_tags to filter by checkpoint tags (private, archived, etc.).
        """
        import json as _json
        # Fetch extra rows when filtering so we can hit the limit after tag pruning
        fetch_limit = limit * 5 if (tags or exclude_tags) else limit
        # Sanitize query for FTS5 — wrap in quotes to treat as phrase,
        # escaping any embedded quotes. This prevents hyphens, colons, etc.
        # from being interpreted as FTS5 operators.
        safe_query = '"{}"'.format(query.replace('"', '""'))
        rows = self._conn.execute(
            """
            SELECT f.id, f.project, f.current_task, f.goal, c.timestamp, c.data
            FROM checkpoints_fts f
            JOIN checkpoints c ON c.id = f.id
            WHERE checkpoints_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, fetch_limit),
        ).fetchall()

        results = []
        for r in rows:
            if tags or exclude_tags:
                cp_tags = _json.loads(r["data"]).get("tags", [])
                if tags and not any(t in cp_tags for t in tags):
                    continue
                if exclude_tags and any(t in cp_tags for t in exclude_tags):
                    continue
            results.append({
                "id": r["id"],
                "project": r["project"],
                "task": r["current_task"],
                "goal": r["goal"],
                "timestamp": r["timestamp"],
            })
            if len(results) >= limit:
                break
        return results

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

    # ------------------------------------------------------------------
    # Project configs (output dirs for sync_files, etc.)
    # ------------------------------------------------------------------

    def get_project_config(self, project: str) -> Optional[ProjectConfig]:
        row = self._conn.execute(
            "SELECT data FROM project_configs WHERE project=?", (project,)
        ).fetchone()
        return ProjectConfig.model_validate_json(row["data"]) if row else None

    def save_project_config(self, config: ProjectConfig) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO project_configs(project, data) VALUES (?,?)",
            (config.project, config.model_dump_json()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Observations (auto-observe)
    # ------------------------------------------------------------------

    def save_observation(self, project: str, tool_name: str, summary: str) -> str:
        obs_id = str(uuid.uuid4())[:8]
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO observations(id, project, tool_name, summary, timestamp, compressed)"
            " VALUES (?,?,?,?,?,0)",
            (obs_id, project, tool_name, summary, ts),
        )
        self._conn.commit()
        return obs_id

    def get_uncompressed_observations(self, project: str, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, tool_name, summary, timestamp FROM observations"
            " WHERE project=? AND compressed=0 ORDER BY timestamp ASC LIMIT ?",
            (project, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def projects_needing_compression(self, threshold: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT project, COUNT(*) as n FROM observations WHERE compressed=0"
            " GROUP BY project HAVING n >= ?",
            (threshold,),
        ).fetchall()
        return [r["project"] for r in rows]

    def mark_observations_compressed(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE observations SET compressed=1 WHERE id IN ({placeholders})", ids
        )
        self._conn.commit()

    def observation_stats(self, project: Optional[str] = None) -> dict:
        if project:
            row = self._conn.execute(
                "SELECT COUNT(*) as total,"
                " SUM(CASE WHEN compressed=0 THEN 1 ELSE 0 END) as pending"
                " FROM observations WHERE project=?",
                (project,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) as total,"
                " SUM(CASE WHEN compressed=0 THEN 1 ELSE 0 END) as pending"
                " FROM observations",
            ).fetchone()
        return {"total": row["total"] or 0, "pending": row["pending"] or 0}

    # ------------------------------------------------------------------
    # Pattern learning
    # ------------------------------------------------------------------

    def upsert_pattern(self, p: PatternSuggestion) -> None:
        existing = self._conn.execute(
            "SELECT id, frequency FROM patterns WHERE project=? AND pattern_key=?",
            (p.project, p.pattern_key),
        ).fetchone()
        if existing:
            new_freq = max(existing["frequency"], p.frequency)
            self._conn.execute(
                "UPDATE patterns SET frequency=?, data=?, last_seen=? WHERE id=?",
                (new_freq, p.model_dump_json(), p.last_seen.isoformat(), existing["id"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO patterns(id, project, pattern_key, frequency, data, last_seen)"
                " VALUES (?,?,?,?,?,?)",
                (p.id, p.project, p.pattern_key, p.frequency,
                 p.model_dump_json(), p.last_seen.isoformat()),
            )
        self._conn.commit()

    def get_patterns(self, project: str, min_frequency: int = 5) -> list[PatternSuggestion]:
        rows = self._conn.execute(
            "SELECT data FROM patterns WHERE project=? AND frequency >= ?"
            " ORDER BY frequency DESC",
            (project, min_frequency),
        ).fetchall()
        return [PatternSuggestion.model_validate_json(r["data"]) for r in rows]

    def all_project_patterns(self, min_frequency: int = 5) -> list[PatternSuggestion]:
        rows = self._conn.execute(
            "SELECT data FROM patterns WHERE frequency >= ? ORDER BY frequency DESC",
            (min_frequency,),
        ).fetchall()
        return [PatternSuggestion.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------
    # Token events
    # ------------------------------------------------------------------

    def save_token_event(
        self, project: Optional[str], used: int, limit: int, pct: float
    ) -> str:
        ev_id = str(uuid.uuid4())[:8]
        self._conn.execute(
            "INSERT INTO token_events(id, project, used, limit_, pct, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (ev_id, project, used, limit, pct, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return ev_id

    def token_history(self, project: Optional[str] = None, limit: int = 20) -> list[dict]:
        if project:
            rows = self._conn.execute(
                "SELECT * FROM token_events WHERE project=? ORDER BY timestamp DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM token_events ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Notification config (per-project + per-task)
    # ------------------------------------------------------------------

    def save_notify_config(self, project: str, config_json: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO notify_configs(project, data) VALUES (?,?)",
            (project, config_json),
        )
        self._conn.commit()

    def get_notify_config(self, project: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT data FROM notify_configs WHERE project=?", (project,)
        ).fetchone()
        return row["data"] if row else None

    def save_task_notify(self, task_id: str, config_json: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO task_notify(task_id, data) VALUES (?,?)",
            (task_id, config_json),
        )
        self._conn.commit()

    def pop_task_notify(self, task_id: str) -> Optional[str]:
        """Return and delete the notify config for a task (called after firing)."""
        row = self._conn.execute(
            "SELECT data FROM task_notify WHERE task_id=?", (task_id,)
        ).fetchone()
        if row:
            self._conn.execute("DELETE FROM task_notify WHERE task_id=?", (task_id,))
            self._conn.commit()
            return row["data"]
        return None

    # -----------------------------------------------------------------------
    # User Memory
    # -----------------------------------------------------------------------

    def remember_user(self, mem: UserMemory) -> None:
        """Upsert a user memory by key."""
        now = datetime.now(timezone.utc).isoformat()
        data = mem.model_dump_json()
        self._conn.execute(
            """INSERT INTO user_memory(id, key, category, source, data, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   id=excluded.id, category=excluded.category,
                   source=excluded.source, data=excluded.data,
                   updated_at=excluded.updated_at""",
            (mem.id, mem.key, mem.category, mem.source, data, now),
        )
        tags_str = " ".join(mem.tags)
        self._conn.execute("DELETE FROM user_memory_fts WHERE id = ?", (mem.id,))
        self._conn.execute(
            "INSERT INTO user_memory_fts(id, key, value, category, tags) VALUES(?,?,?,?,?)",
            (mem.id, mem.key, mem.value, mem.category, tags_str),
        )
        self._conn.commit()

    def recall_user(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search user memories via FTS or category filter."""
        if query:
            sql = """
                SELECT um.data FROM user_memory um
                JOIN user_memory_fts fts ON um.id = fts.id
                WHERE user_memory_fts MATCH ?
            """
            params: list = [query]
            if category:
                sql += " AND um.category = ?"
                params.append(category)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT data FROM user_memory"
            params = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            m = UserMemory.model_validate_json(row[0])
            results.append({"id": m.id, "key": m.key, "value": m.value,
                            "category": m.category, "source": m.source, "tags": m.tags})
        return results

    def get_user_memory(self, key: str) -> Optional[UserMemory]:
        row = self._conn.execute(
            "SELECT data FROM user_memory WHERE key = ?", (key,)
        ).fetchone()
        return UserMemory.model_validate_json(row[0]) if row else None

    def delete_user_memory(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT id FROM user_memory WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM user_memory WHERE key = ?", (key,))
        self._conn.execute("DELETE FROM user_memory_fts WHERE id = ?", (row[0],))
        self._conn.commit()
        return True

    def all_user_memory(self, category: Optional[str] = None) -> list[UserMemory]:
        if category:
            rows = self._conn.execute(
                "SELECT data FROM user_memory WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM user_memory ORDER BY updated_at DESC"
            ).fetchall()
        return [UserMemory.model_validate_json(r[0]) for r in rows]

    # -----------------------------------------------------------------------
    # Agent Memory
    # -----------------------------------------------------------------------

    def remember_agent(self, mem: AgentMemory) -> None:
        """Upsert an agent behavioral memory by key."""
        now = datetime.now(timezone.utc).isoformat()
        data = mem.model_dump_json()
        self._conn.execute(
            """INSERT INTO agent_memory(id, key, category, source, data, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   id=excluded.id, category=excluded.category,
                   source=excluded.source, data=excluded.data,
                   updated_at=excluded.updated_at""",
            (mem.id, mem.key, mem.category, mem.source, data, now),
        )
        rationale_str = mem.rationale or ""
        self._conn.execute("DELETE FROM agent_memory_fts WHERE id = ?", (mem.id,))
        self._conn.execute(
            "INSERT INTO agent_memory_fts(id, key, value, category, rationale) VALUES(?,?,?,?,?)",
            (mem.id, mem.key, mem.value, mem.category, rationale_str),
        )
        self._conn.commit()

    def recall_agent(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search agent memories via FTS or category filter."""
        if query:
            sql = """
                SELECT am.data FROM agent_memory am
                JOIN agent_memory_fts fts ON am.id = fts.id
                WHERE agent_memory_fts MATCH ?
            """
            params: list = [query]
            if category:
                sql += " AND am.category = ?"
                params.append(category)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT data FROM agent_memory"
            params = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            m = AgentMemory.model_validate_json(row[0])
            results.append({"id": m.id, "key": m.key, "value": m.value,
                            "category": m.category, "source": m.source,
                            "rationale": m.rationale, "tags": m.tags})
        return results

    def get_agent_memory(self, key: str) -> Optional[AgentMemory]:
        row = self._conn.execute(
            "SELECT data FROM agent_memory WHERE key = ?", (key,)
        ).fetchone()
        return AgentMemory.model_validate_json(row[0]) if row else None

    def delete_agent_memory(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT id FROM agent_memory WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM agent_memory WHERE key = ?", (key,))
        self._conn.execute("DELETE FROM agent_memory_fts WHERE id = ?", (row[0],))
        self._conn.commit()
        return True

    def all_agent_memory(self, category: Optional[str] = None) -> list[AgentMemory]:
        if category:
            rows = self._conn.execute(
                "SELECT data FROM agent_memory WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT data FROM agent_memory ORDER BY updated_at DESC"
            ).fetchall()
        return [AgentMemory.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        self._conn.close()
