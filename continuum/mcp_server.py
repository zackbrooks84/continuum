"""
Continuum MCP server — persistent memory + async execution for Claude Code agents.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NOT SURE WHERE TO START?  Call quickstart() — it returns this guide live.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

THREE PATTERNS — cover 90% of use cases:

  Pattern 1 · Resume a project (start of every session)
    smart_resume("myapp")
    → auto-searches memory, picks the right depth, returns context

  Pattern 2 · Run + remember (push work and checkpoint together)
    push_and_checkpoint("pytest tests/", project="myapp", ...)
    → queues the task AND saves current state in one call

  Pattern 3 · Full automation (set once, never think about it again)
    auto_mode(enabled=True, project="myapp")
    → activates auto-observe + auto-sync + smart retrieval simultaneously

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TOOL TIERS:

  [CORE] — use these every session:
    quickstart          — orientation guide + tiered tool inventory (call this first)
    north_star          — unified session briefing: user identity + agent protocols + projects
    remember_me         — store a user fact (bio, preference, rule)
    recall_me           — retrieve user facts by FTS search or category
    remember_this       — store an agent behavioral protocol or constraint
    recall_this         — retrieve agent protocols by FTS search
    forget              — delete a user or agent memory
    smart_resume        — resume a project: auto-depth 3-tier memory retrieval
    checkpoint          — save work state: goal, findings, decisions, next_steps
    handoff             — compact resume briefing (<1k tokens) for end of session
    auto_mode           — activate all automation layers at once

  [COMMON] — reach for these regularly:
    forge_push          — queue a shell command to run in the background
    push_and_checkpoint — queue a task AND checkpoint current state together
    remember            — multi-project briefing for session start (~300 tokens)
    notify_configure    — set up Telegram/Discord/Slack alerts on task complete
    observe_control     — turn passive tool-call capture on/off/status
    sync_files          — write MEMORY.md / DECISIONS.md / TASKS.md to disk
    forge_status        — check task queue or specific task status
    forge_result        — get full output of a completed task

  [ADVANCED] — power features, use when you need them:
    memory_search       — manual FTS search (smart_resume wraps this automatically)
    memory_timeline     — chronological context slice around a checkpoint
    memory_get          — full details for specific checkpoint IDs
    forge_list          — list tasks by status filter
    forge_cancel        — cancel a pending task
    forge_run_now       — run a task immediately, no daemon needed
    status              — latest checkpoint for a project
    history             — list recent checkpoints
    projects            — list all tracked projects
    export_handoff      — export handoff as portable markdown + token preview
    import_web_handoff  — import a web-pasted handoff back into local DB
    dispatch_claude     — run a headless `claude --print` session as a task
    token_watch         — live context budget tracking + auto-checkpoint at threshold
    cross_agent_handoff — handoff formatted for gpt/gemini/generic agents
    pattern_suggestions — surface recurring decision patterns (fires after 5+ similar)
    notify_when_complete — one-time task notification (fires and self-deletes)
    notify_test         — test notification channels with a test message

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Run via:
    uv run --project /path/to/continuum python -m continuum.mcp_server

Environment variables:
    CONTINUUM_AUTO_MODE=1           activate all automation layers on startup
    CONTINUUM_AUTO_OBSERVE=1        enable auto-observe globally
    CONTINUUM_OBSERVE_PROJECT=name  default project for observations
    CONTINUUM_OBSERVE_METHOD=claude use Claude Haiku to compress (needs ANTHROPIC_API_KEY)
    CONTINUUM_REMOTE_TOKEN=...      Bearer token for remote HTTP server
    CONTINUUM_TG_TOKEN=...          Telegram bot token (from @BotFather)
    CONTINUUM_TG_CHAT=...           Telegram chat ID  (from @userinfobot)
    CONTINUUM_DISCORD_WEBHOOK=...   Discord incoming webhook URL
    CONTINUUM_SLACK_WEBHOOK=...     Slack incoming webhook URL
    CONTINUUM_WEBHOOK_URL=...       Generic POST webhook URL
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from statistics import mean
from time import perf_counter
from typing import Any, List, Optional

from fastmcp import FastMCP

from pathlib import Path
from .models import Task, TaskStatus, Checkpoint, Decision, ProjectConfig, UserMemory, AgentMemory, MemoryCategory, AgentMemoryCategory
from .db import DB
from .handoff import generate_handoff
from .daemon import daemon_status
from .runner import run_task, check_resources
from .observer import summarize_call

mcp = FastMCP("continuum", "0.1.0")
_db = DB()

# ---------------------------------------------------------------------------
# Session-level state
# Seeded from env vars; toggled at runtime via auto_observe_toggle() / auto_mode()
# ---------------------------------------------------------------------------
_auto_observe: bool = bool(os.environ.get("CONTINUUM_AUTO_OBSERVE"))
_observe_project: Optional[str] = os.environ.get("CONTINUUM_OBSERVE_PROJECT")
_observe_method: str = os.environ.get("CONTINUUM_OBSERVE_METHOD", "rule")
_auto_mode_active: bool = bool(os.environ.get("CONTINUUM_AUTO_MODE"))
_checkpoint_timings: list[dict[str, float]] = []
_forge_push_timings: list[dict[str, float]] = []
_profile_forge_push: bool = bool(os.environ.get("CONTINUUM_PROFILE_FORGE_PUSH"))


def _maybe_sync(project: str) -> None:
    """Regenerate markdown files for a project.

    Uses the configured output_dir if set; otherwise falls back to
    ~/.continuum/projects/{project}/ so auto-sync always runs — no config step required.
    """
    try:
        from .sync_files import sync_project_files
        cp = _db.latest_checkpoint(project)
        if not cp:
            return
        config = _db.get_project_config(project)
        if config and not config.auto_sync:
            return  # explicitly disabled
        if config and config.output_dir:
            out_path = Path(config.output_dir)
        else:
            # Default: always sync to ~/.continuum/projects/{project}/
            out_path = Path.home() / ".continuum" / "projects" / project
        history = _db.list_checkpoints(project=project, limit=25)
        sync_project_files(cp, history, out_path)
    except Exception:
        pass


def _maybe_observe(tool_name: str, kwargs: dict, result: Any) -> None:
    """If auto-observe is active, quietly record a semantic summary of this tool call."""
    if not _auto_observe:
        return
    proj = kwargs.get("project") or _observe_project
    if not proj:
        return
    try:
        summary = summarize_call(tool_name, kwargs, result)
        _db.save_observation(proj, tool_name, summary)
    except Exception:
        pass


def _post_checkpoint_side_effects(project: str, task: str, status: str, result: dict) -> None:
    _maybe_sync(project)
    _maybe_observe("checkpoint", {"project": project, "task": task, "status": status}, result)


def checkpoint_timing_snapshot(reset: bool = False) -> dict:
    """Return aggregated timing stats for recent checkpoint() calls.

    Intended for internal benchmarking and debugging.
    """
    if not _checkpoint_timings:
        return {"count": 0, "stages": {}}
    keys = _checkpoint_timings[0].keys()
    stages: dict[str, dict[str, float]] = {}
    for key in keys:
        values = [row[key] for row in _checkpoint_timings]
        values_sorted = sorted(values)
        mid = len(values_sorted) // 2
        if len(values_sorted) % 2:
            median = values_sorted[mid]
        else:
            median = (values_sorted[mid - 1] + values_sorted[mid]) / 2
        stages[key] = {
            "mean_ms": mean(values) * 1000.0,
            "median_ms": median * 1000.0,
            "min_ms": values_sorted[0] * 1000.0,
            "max_ms": values_sorted[-1] * 1000.0,
        }
    snapshot = {"count": len(_checkpoint_timings), "stages": stages}
    if reset:
        _checkpoint_timings.clear()
    return snapshot


def forge_push_timing_snapshot(reset: bool = False) -> dict:
    """Return aggregated timing stats for recent forge_push() calls."""
    if not _forge_push_timings:
        return {"count": 0, "stages": {}}
    keys = _forge_push_timings[0].keys()
    stages: dict[str, dict[str, float]] = {}
    for key in keys:
        values = [row[key] for row in _forge_push_timings]
        values_sorted = sorted(values)
        mid = len(values_sorted) // 2
        if len(values_sorted) % 2:
            median = values_sorted[mid]
        else:
            median = (values_sorted[mid - 1] + values_sorted[mid]) / 2
        stages[key] = {
            "mean_ms": mean(values) * 1000.0,
            "median_ms": median * 1000.0,
            "min_ms": values_sorted[0] * 1000.0,
            "max_ms": values_sorted[-1] * 1000.0,
        }
    snapshot = {"count": len(_forge_push_timings), "stages": stages}
    if reset:
        _forge_push_timings.clear()
    return snapshot


# ===========================================================================
# Orientation — call this first if you're not sure what to do
# ===========================================================================

@mcp.tool()
def quickstart(project: Optional[str] = None) -> dict:
    """[CORE] Orientation guide — call this first if you're not sure where to start.

    Returns the three core patterns, a tiered tool inventory, and (if project
    is provided) the current state of that project.

    Three patterns cover 90% of use cases:
      1. Resume    → smart_resume("project")
      2. Run+save  → push_and_checkpoint("command", project="x")
      3. Automate  → auto_mode(enabled=True, project="x")
    """
    guide = {
        "three_patterns": {
            "1_resume": {
                "call": "smart_resume('myapp')",
                "when": "Start of every session — reloads context automatically",
                "depth": "Auto-selects: full detail (1-3 hits) / timeline (4-10) / summaries (10+)",
            },
            "2_run_and_remember": {
                "call": "push_and_checkpoint('pytest tests/', project='myapp', current_task='...', goal='...')",
                "when": "Push background work AND save state in one call",
                "output": "task_id + checkpoint_id",
            },
            "3_full_automation": {
                "call": "auto_mode(enabled=True, project='myapp')",
                "when": "Set once — activates all 3 layers and runs forever",
                "layers": [
                    "auto-observe: every tool call silently captured",
                    "auto-sync: MEMORY/DECISIONS/TASKS.md updated on every checkpoint",
                    "smart retrieval: handoff() auto-injects relevant history",
                ],
            },
        },
        "tool_tiers": {
            "CORE — use every session": [
                "quickstart        — this guide",
                "north_star        — call FIRST each session: user identity + protocols + projects briefing",
                "smart_resume      — resume with auto-depth memory retrieval",
                "checkpoint        — save goal / findings / decisions / next_steps",
                "handoff           — compact end-of-session briefing (<1k tokens)",
                "auto_mode         — activate all automation layers at once",
            ],
            "COMMON — reach for regularly": [
                "forge_push        — queue a shell command in the background",
                "push_and_checkpoint — queue task + save checkpoint together",
                "remember          — multi-project briefing (~300 tokens)",
                "notify_configure  — Telegram/Discord/Slack alerts on task complete",
                "observe_control   — passive capture on/off/status",
                "sync_files        — write MEMORY.md / DECISIONS.md / TASKS.md",
                "forge_status      — check task queue or specific task",
                "forge_result      — full output of a completed task",
            ],
            "ADVANCED — power features": [
                "memory_search     — manual FTS (smart_resume wraps this)",
                "memory_timeline   — chronological slice around a checkpoint",
                "memory_get        — full detail for specific checkpoint IDs",
                "token_watch       — live context budget + auto-checkpoint at 80%",
                "export_handoff    — portable markdown export with token preview",
                "import_web_handoff — import a web-pasted handoff",
                "dispatch_claude   — run headless `claude --print` as a task",
                "cross_agent_handoff — handoff for gpt/gemini/generic agents",
                "pattern_suggestions — recurring decision patterns after 5+ similar",
                "notify_when_complete — one-time task notification",
                "forge_list / forge_cancel / forge_run_now / history / projects / status",
            ],
        },
        "tip": (
            "Start every session with north_star() — it returns your user identity, "
            "agent protocols, and active projects in one <500-token briefing. "
            "Then call smart_resume('yourproject') or auto_mode(enabled=True, project='yourproject')."
        ),
    }

    if project:
        cp = _db.latest_checkpoint(project)
        guide["project_state"] = {
            "project": project,
            "has_checkpoint": cp is not None,
            "latest_task": cp.current_task if cp else None,
            "status": cp.status if cp else None,
            "timestamp": cp.timestamp.isoformat() if cp else None,
            "next_steps": cp.next_steps[:3] if cp else [],
            "tip": (
                f"Call smart_resume('{project}') to load full context."
                if cp else
                f"No checkpoints yet. Call checkpoint(project='{project}', ...) to start tracking."
            ),
        }

    return guide


# ===========================================================================
# Task Queue
# ===========================================================================

@mcp.tool()
def forge_push(
    name: str,
    command: str,
    cwd: Optional[str] = None,
    project: Optional[str] = None,
    auto_checkpoint: bool = False,
    priority: int = 5,
    depends_on: Optional[str] = None,
    timeout: Optional[int] = None,
    min_free_ram_mb: int = 256,
    tags: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """[COMMON] Push a shell command onto the Continuum task queue.

    The daemon picks it up and runs it offline — you can close your session.
    Set auto_checkpoint=True with a project name to automatically save a
    structured checkpoint when the task completes.

    Safety: set dry_run=True to preview what would be queued without actually
    running anything. Set CONTINUUM_SAFE_MODE=confirm in your environment to
    require forge_approve() before any task executes.

    Args:
        name: Human-readable task name
        command: Shell command to execute
        cwd: Working directory (default: home dir)
        project: Project identifier for checkpointing results
        auto_checkpoint: Parse stdout into a checkpoint on completion
        priority: 1 (run first) to 10 (run last), default 5
        depends_on: Task ID that must complete before this starts
        timeout: Max seconds to run (None = unlimited)
        min_free_ram_mb: Skip task if insufficient RAM (default 256)
        tags: Optional labels for filtering
        dry_run: If True, preview the task without queuing it
    """
    total_start = perf_counter() if _profile_forge_push else 0.0
    validation_start = total_start
    safe_mode = os.environ.get("CONTINUUM_SAFE_MODE", "").lower()
    validation_elapsed = (perf_counter() - validation_start) if _profile_forge_push else 0.0

    if dry_run:
        return {
            "dry_run": True,
            "would_queue": {
                "name": name,
                "command": command,
                "cwd": cwd or str(Path.home()),
                "project": project,
                "auto_checkpoint": auto_checkpoint,
                "priority": priority,
                "depends_on": depends_on,
            },
            "message": "Dry run — nothing queued. Remove dry_run=True to execute.",
        }

    # Determine initial status based on safe mode
    from .models import TaskStatus
    status = TaskStatus.WAITING if safe_mode == "confirm" else TaskStatus.PENDING

    task_create_start = perf_counter() if _profile_forge_push else 0.0
    now = datetime.now(timezone.utc)
    task = Task.model_construct(
        id=str(uuid.uuid4())[:8],
        name=name,
        command=command,
        cwd=cwd,
        env={},
        status=status,
        priority=priority,
        depends_on=depends_on,
        project=project,
        auto_checkpoint=auto_checkpoint,
        created_at=now,
        started_at=None,
        finished_at=None,
        timeout=timeout,
        min_free_ram_mb=min_free_ram_mb,
        tags=list(tags or []),
    )
    task_create_elapsed = (perf_counter() - task_create_start) if _profile_forge_push else 0.0

    db_start = perf_counter() if _profile_forge_push else 0.0
    _db.push_task(task)
    db_elapsed = (perf_counter() - db_start) if _profile_forge_push else 0.0
    db_timing = (_db.latest_push_task_timing() or {}) if _profile_forge_push else {}
    lock_elapsed = float(db_timing.get("lock_wait_s", 0.0)) if _profile_forge_push else 0.0

    if safe_mode == "confirm":
        result = {
            "task_id": task.id,
            "name": task.name,
            "status": "pending_confirm",
            "safe_mode": "confirm",
            "message": "Task queued but WILL NOT RUN until approved.",
            "approve_with": f"forge_approve(task_id='{task.id}')",
            "ui_approve": f"http://localhost:8765/tasks/{task.id}/approve",
        }
    else:
        result = {
            "task_id": task.id,
            "name": task.name,
            "status": task.status.value,
            "auto_checkpoint": task.auto_checkpoint,
            "tip": "Start the daemon with: continuum daemon start",
        }

    side_effect_start = perf_counter() if _profile_forge_push else 0.0
    _maybe_observe("forge_push", {"name": name, "command": command, "project": project}, result)
    if _profile_forge_push:
        side_effect_elapsed = perf_counter() - side_effect_start
        total_elapsed = perf_counter() - total_start
        _forge_push_timings.append({
            "input_validation_s": validation_elapsed,
            "task_object_creation_s": task_create_elapsed,
            "db_insert_commit_s": db_elapsed,
            "lock_acquisition_s": lock_elapsed,
            "sync_side_effects_s": side_effect_elapsed,
            "total_s": total_elapsed,
        })
    return result


@mcp.tool()
def forge_status(task_id: Optional[str] = None) -> dict:
    """[COMMON] Get status of a specific task, or overall queue stats + daemon status.

    Args:
        task_id: Specific task ID, or None for queue summary
    """
    if task_id:
        task = _db.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        out = task.model_dump(mode="json")
        result = _db.get_result(task_id)
        if result:
            out["result"] = {
                "exit_code": result.exit_code,
                "success": result.success,
                "duration_seconds": result.duration_seconds,
                "stdout_tail": result.stdout[-500:],
                "stderr_tail": result.stderr[-300:],
            }
        return out
    else:
        return {"queue": _db.task_stats(), "daemon": daemon_status()}


@mcp.tool()
def forge_list(status: Optional[str] = None, limit: int = 20) -> list[dict]:
    """List tasks in the queue.

    Args:
        status: Filter by status (pending/running/done/failed/cancelled), or None for all
        limit: Max tasks to return
    """
    tasks = _db.list_tasks(status=status, limit=limit)
    return [
        {
            "id": t.id, "name": t.name, "status": t.status.value,
            "priority": t.priority, "project": t.project,
            "auto_checkpoint": t.auto_checkpoint,
            "created_at": t.created_at.isoformat(),
        }
        for t in tasks
    ]


@mcp.tool()
def forge_result(task_id: str) -> dict:
    """[COMMON] Get the full stdout/stderr of a completed task.

    Args:
        task_id: Task ID to retrieve result for
    """
    result = _db.get_result(task_id)
    if not result:
        task = _db.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {"task_id": task_id, "status": task.status.value, "result": None}
    return result.model_dump(mode="json")


@mcp.tool()
def forge_cancel(task_id: str) -> dict:
    """Cancel a pending or waiting task.

    Args:
        task_id: Task ID to cancel
    """
    ok = _db.cancel_task(task_id)
    return {"cancelled": ok, "task_id": task_id}


@mcp.tool()
def forge_approve(task_id: str) -> dict:
    """[COMMON] Approve a task that was queued in safe-mode confirm.

    When CONTINUUM_SAFE_MODE=confirm is set, forge_push() queues tasks in
    a 'waiting' state. Call forge_approve() to release the task so the
    daemon will actually run it.

    Also available in the web UI: http://localhost:8765/tasks/{task_id}/approve

    Args:
        task_id: Task ID to approve
    """
    task = _db.get_task(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}
    if task.status not in (TaskStatus.PENDING, TaskStatus.WAITING):
        return {"error": f"Task is already {task.status.value}"}
    task.status = TaskStatus.PENDING
    _db.update_task(task)
    return {
        "approved": True,
        "task_id": task_id,
        "name": task.name,
        "message": "Task is now queued. The daemon will pick it up on the next cycle.",
    }


@mcp.tool()
def safe_mode(level: str = "off", project: Optional[str] = None) -> dict:
    """[COMMON] Set the safety level for task execution — controls what runs automatically.

    Levels:
      "off"     — default; tasks run immediately when queued (no friction)
      "preview" — forge_push() always acts as dry_run; nothing queues without explicit confirm
      "confirm" — tasks queue as waiting; require forge_approve() before daemon runs them
      "sandbox" — tasks queue as waiting AND command is shown for review before approval

    The level is set via CONTINUUM_SAFE_MODE env var for the current session.
    This tool sets it for the current process only; export the var for persistence.

    Web UI shows approve buttons for all waiting tasks: http://localhost:8765/tasks

    Args:
        level: "off" | "preview" | "confirm" | "sandbox"
        project: Optional project scope (future: per-project configs)
    """
    import os
    valid = {"off", "preview", "confirm", "sandbox"}
    if level not in valid:
        return {"error": f"Invalid level '{level}'. Choose from: {', '.join(sorted(valid))}"}

    os.environ["CONTINUUM_SAFE_MODE"] = "" if level == "off" else level

    descriptions = {
        "off":     "Tasks run immediately. No confirmation needed.",
        "preview": "forge_push() shows a preview — use dry_run=False to actually queue.",
        "confirm": "Tasks queue as waiting. Call forge_approve(task_id) to run.",
        "sandbox": "Tasks queue as waiting AND command is shown for review.",
    }

    return {
        "safe_mode": level,
        "project": project,
        "description": descriptions[level],
        "env_var": f"CONTINUUM_SAFE_MODE={level}",
        "tip": "Export CONTINUUM_SAFE_MODE in your shell profile to make this permanent.",
        "ui": "http://localhost:8765/tasks — approve waiting tasks in the browser",
    }


@mcp.tool()
def forge_run_now(task_id: str) -> dict:
    """Run a specific pending task immediately in the foreground.
    Useful when the daemon isn't running.

    Args:
        task_id: Task ID to run
    """
    task = _db.get_task(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}
    if task.status not in (TaskStatus.PENDING, TaskStatus.WAITING):
        return {"error": f"Task is {task.status.value}, can only run pending/waiting tasks"}

    ok, reason = check_resources(task.min_free_ram_mb)
    if not ok:
        return {"error": f"Insufficient resources: {reason}"}

    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    _db.update_task(task)

    result = run_task(task)

    task.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
    task.finished_at = datetime.now(timezone.utc)
    _db.update_task(task)
    _db.save_result(result)

    from .runner import auto_checkpoint
    auto_checkpoint(task, result)

    return {
        "task_id": task.id,
        "success": result.success,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-500:],
    }


# ===========================================================================
# Checkpoints
# ===========================================================================

@mcp.tool()
def checkpoint(
    project: str,
    task: str,
    goal: str,
    status: str = "in-progress",
    context: str = "",
    findings: Optional[list[str]] = None,
    dead_ends: Optional[list[str]] = None,
    next_steps: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    files_changed: Optional[list[str]] = None,
    decisions: Optional[list[dict]] = None,
    agent: Optional[str] = None,
) -> dict:
    """[CORE] Save a checkpoint of your current work state.

    Call this at natural break points — before closing a session, when
    blocked, after a major finding, or before handing off to another agent.
    The checkpoint survives session death and context compaction.

    Args:
        project: Project identifier (e.g. 'myapp-auth', 'research-llm')
        task: What you are doing RIGHT NOW, concisely
        goal: The overall goal this work is driving toward
        status: 'in-progress' | 'blocked' | 'complete' | 'abandoned'
        context: Background, constraints, or relevant history
        findings: Things learned that are useful to preserve
        dead_ends: Approaches tried and ruled out — future agents won't repeat them
        next_steps: Ordered list of what to do next (first = most immediate)
        open_questions: Unresolved questions that need answers
        files_changed: Files created or modified in this work
        decisions: List of {what, why, alternatives_rejected} dicts
        agent: Your agent identifier (e.g. 'claude-sonnet-4-6')
    """
    validate_start = perf_counter()
    parsed_decisions = [
        Decision(
            what=d.get("what", ""),
            why=d.get("why", ""),
            alternatives_rejected=d.get("alternatives_rejected", []),
        )
        for d in (decisions or [])
    ]

    cp = Checkpoint(
        project=project,
        current_task=task,
        goal=goal,
        status=status,
        context=context,
        findings=findings or [],
        dead_ends=dead_ends or [],
        decisions=parsed_decisions,
        next_steps=next_steps or [],
        open_questions=open_questions or [],
        files_changed=files_changed or [],
        agent=agent,
    )
    validate_elapsed = perf_counter() - validate_start

    save_start = perf_counter()
    _db.save_checkpoint(cp)
    save_elapsed = perf_counter() - save_start
    db_timing = _db.latest_checkpoint_timing() or {}
    result = {
        "checkpoint_id": cp.id,
        "project": cp.project,
        "saved_at": cp.timestamp.isoformat(),
        "message": f"Checkpoint saved for '{project}'. Resume with: continuum resume {project}",
    }

    side_effect_start = perf_counter()
    _post_checkpoint_side_effects(project, task, status, result)
    side_effect_elapsed = perf_counter() - side_effect_start
    _checkpoint_timings.append({
        "model_validation_s": validate_elapsed,
        "serialization_s": db_timing.get("serialization_s", 0.0),
        "sql_execute_s": db_timing.get("sql_execute_s", 0.0),
        "commit_s": db_timing.get("commit_s", 0.0),
        "save_checkpoint_total_s": save_elapsed,
        "post_write_enqueue_s": side_effect_elapsed,
    })
    return result


@mcp.tool()
def handoff(
    project: str,
    checkpoint_id: Optional[str] = None,
    target_agent: Optional[str] = None,
    save: bool = True,
) -> dict:
    """[CORE] Generate a compact resume briefing (<1k tokens) for a new agent session.

    Load this at the start of a new session to instantly understand where
    you left off — without re-reading logs or re-deriving context.

    Args:
        project: Project to generate handoff for
        checkpoint_id: Specific checkpoint (default: latest)
        target_agent: Who receives the handoff (informational)
        save: Persist the handoff document
    """
    if checkpoint_id:
        cp = _db.get_checkpoint(checkpoint_id)
        if not cp:
            return {"error": f"Checkpoint not found: {checkpoint_id}"}
    else:
        cp = _db.latest_checkpoint(project)
        if not cp:
            return {"error": f"No checkpoints for project '{project}'"}

    h = generate_handoff(cp, target_agent=target_agent)
    if save:
        _db.save_handoff(h)

    result = {
        "handoff_id": h.id,
        "checkpoint_id": h.checkpoint_id,
        "token_estimate": h.token_estimate,
        "executive_summary": h.executive_summary,
        "full_context": h.full_context,
        "immediate_action": h.immediate_action,
        "watch_out_for": h.watch_out_for,
    }
    if h.token_estimate > 1500:
        result["token_warning"] = (
            f"This handoff is ~{h.token_estimate} tokens. "
            "Consider using memory_search() + memory_get() for targeted retrieval instead."
        )
    _maybe_sync(project)
    _maybe_observe("handoff", {"project": project}, result)
    return result


@mcp.tool()
def status(project: str) -> dict:
    """Get the latest checkpoint state for a project.

    Args:
        project: Project identifier
    """
    cp = _db.latest_checkpoint(project)
    if not cp:
        return {"error": f"No checkpoints for project '{project}'"}
    return {
        "project": cp.project,
        "current_task": cp.current_task,
        "goal": cp.goal,
        "status": cp.status,
        "timestamp": cp.timestamp.isoformat(),
        "next_steps": cp.next_steps,
        "open_questions": cp.open_questions,
        "findings": cp.findings,
        "dead_ends": cp.dead_ends,
        "checkpoint_id": cp.id,
        "agent": cp.agent,
    }


@mcp.tool()
def history(
    project: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """List recent checkpoints, optionally filtered by project.

    Args:
        project: Filter by project (None = all projects)
        limit: Max checkpoints to return
    """
    checkpoints = _db.list_checkpoints(project=project, limit=limit)
    return {
        "checkpoints": [
            {
                "id": cp.id,
                "short_id": cp.id[:8],
                "project": cp.project,
                "task": cp.current_task,
                "status": cp.status,
                "timestamp": cp.timestamp.isoformat(),
                "agent": cp.agent,
            }
            for cp in checkpoints
        ]
    }


@mcp.tool()
def projects() -> dict:
    """List all projects with checkpoints, with latest status."""
    project_names = _db.list_projects()
    result = []
    for name in project_names:
        cp = _db.latest_checkpoint(name)
        result.append({
            "project": name,
            "latest_task": cp.current_task if cp else None,
            "latest_status": cp.status if cp else None,
            "latest_timestamp": cp.timestamp.isoformat() if cp else None,
        })
    return {"projects": result}


# ===========================================================================
# Hybrid: push + checkpoint in one call
# ===========================================================================

@mcp.tool()
def push_and_checkpoint(
    name: str,
    command: str,
    project: str,
    goal: str,
    context: str = "",
    next_steps: Optional[list[str]] = None,
    findings: Optional[list[str]] = None,
    cwd: Optional[str] = None,
    priority: int = 5,
    tags: Optional[list[str]] = None,
) -> dict:
    """[CORE] Queue a task AND save a pre-run checkpoint in one call.

    Use this when you're about to go offline: it records your current
    reasoning state AND queues the work to run while you're gone.
    When you return, call smart_resume(project) to reload full context.

    Args:
        name: Task name
        command: Shell command to queue
        project: Project identifier (used for both task and checkpoint)
        goal: Overall goal this work drives toward
        context: Background and constraints
        next_steps: What to do after the task completes
        findings: Current knowledge to preserve
        cwd: Working directory
        priority: Task priority (1-10)
        tags: Labels for filtering
    """
    # Save pre-run checkpoint
    cp = Checkpoint(
        project=project,
        current_task=f"Queued: {name}",
        goal=goal,
        status="in-progress",
        context=context,
        findings=findings or [],
        next_steps=next_steps or [f"Review task output: {name}"],
    )
    _db.save_checkpoint(cp)

    # Queue the task with auto-checkpoint on completion
    task = Task(
        name=name,
        command=command,
        cwd=cwd,
        project=project,
        auto_checkpoint=True,
        priority=priority,
        tags=tags or [],
    )
    _db.push_task(task)

    result = {
        "task_id": task.id,
        "checkpoint_id": cp.id,
        "project": project,
        "message": f"Task queued and checkpoint saved. Resume with: continuum resume {project}",
    }
    _maybe_observe("push_and_checkpoint", {"name": name, "project": project, "goal": goal}, result)
    return result


# ===========================================================================
# Memory — search, timeline, get, remember
# ===========================================================================

@mcp.tool()
def memory_search(
    query: str,
    limit: int = 10,
    tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None,
) -> dict:
    """[ADVANCED] Search across all checkpoints using full-text search.

    Tip: use smart_resume(project) instead — it calls this automatically and
    selects the right depth. Use memory_search() only when you need manual control.

    Returns a compact list of matching checkpoint IDs and short summaries —
    designed to be cheap to scan. Use memory_get() to fetch full details
    for the IDs you actually need.

    Tag filtering lets you scope or exclude private/archived checkpoints:
      tags=["private"]           — only return checkpoints tagged "private"
      exclude_tags=["archived"]  — skip archived checkpoints

    Args:
        query: Search terms (supports FTS5 syntax: quotes, AND/OR/NOT, prefix*)
        limit: Max results to return
        tags: Only return checkpoints that have ALL of these tags
        exclude_tags: Skip checkpoints that have ANY of these tags
    """
    results = _db.search_checkpoints(query, limit=limit, tags=tags, exclude_tags=exclude_tags)
    result = {
        "query": query,
        "count": len(results),
        "tag_filter": {"include": tags, "exclude": exclude_tags},
        "matches": results,
        "tip": "Use memory_get(ids=[...]) to fetch full details for specific checkpoints.",
    }
    _maybe_observe("memory_search", {"query": query}, result)
    return result


@mcp.tool()
def memory_timeline(checkpoint_id: str, window: int = 5) -> dict:
    """[ADVANCED] Return a chronological context slice around a checkpoint.

    Tip: use smart_resume(project) instead — it calls this automatically for
    medium-density results. Use memory_timeline() only for manual control.

    Fetches up to `window` checkpoints before and after the given one
    within the same project — useful for understanding how thinking evolved.

    Args:
        checkpoint_id: The pivot checkpoint ID
        window: Number of checkpoints to fetch on each side (default 5)
    """
    cps = _db.checkpoint_timeline(checkpoint_id, window=window)
    if not cps:
        return {"error": f"Checkpoint {checkpoint_id} not found"}
    return {
        "project": cps[0].project if cps else None,
        "total": len(cps),
        "timeline": [
            {
                "id": cp.id,
                "timestamp": cp.timestamp.isoformat(),
                "task": cp.current_task,
                "status": cp.status,
                "is_pivot": cp.id == checkpoint_id,
            }
            for cp in cps
        ],
        "tip": "Use memory_get(ids=[...]) for full details on any of these.",
    }


@mcp.tool()
def memory_get(ids: list[str]) -> dict:
    """[ADVANCED] Fetch full checkpoint details for a list of IDs.

    Use this after memory_search() or memory_timeline() when you need the
    complete state: all findings, dead ends, decisions, next steps.

    Args:
        ids: List of checkpoint IDs to retrieve
    """
    cps = _db.get_checkpoints_by_ids(ids)
    return {
        "count": len(cps),
        "checkpoints": [
            {
                "id": cp.id,
                "project": cp.project,
                "timestamp": cp.timestamp.isoformat(),
                "task": cp.current_task,
                "goal": cp.goal,
                "status": cp.status,
                "context": cp.context,
                "findings": cp.findings,
                "dead_ends": cp.dead_ends,
                "next_steps": cp.next_steps,
                "open_questions": cp.open_questions,
                "files_changed": cp.files_changed,
                "decisions": [d.model_dump() for d in cp.decisions],
                "agent": cp.agent,
            }
            for cp in cps
        ],
    }


@mcp.tool()
def remember(days: int = 14) -> dict:
    """[COMMON] Auto-inject: compact briefing of all active projects for session start.

    Call this at the beginning of every new session. Returns a ~200-400 token
    summary of what's been going on — project statuses, current tasks, and
    immediate next steps — so you never start cold.

    Add to your .mcp.json 'onStart' to have it fire automatically.

    Args:
        days: How far back to look for active projects (default 14)
    """
    summaries = _db.recent_project_summaries(days=days)
    if not summaries:
        return {
            "active_projects": 0,
            "briefing": "No active projects in the last {} days. Use checkpoint() to start tracking work.".format(days),
        }

    lines = [f"## Active Projects ({len(summaries)} in last {days}d)\n"]
    for s in summaries:
        status_icon = {"in-progress": "◉", "blocked": "✗", "complete": "✓", "abandoned": "○"}.get(s["status"], "?")
        lines.append(f"**{s['project']}** {status_icon} `{s['status']}`")
        lines.append(f"  Goal: {s['goal']}")
        lines.append(f"  Now: {s['task']}")
        if s["next_step"]:
            lines.append(f"  Next: {s['next_step']}")
        if s["dead_ends"]:
            lines.append(f"  Dead ends to avoid: {s['dead_ends']}")
        lines.append(f"  ID: {s['checkpoint_id'][:8]}  Updated: {s['last_updated'][:10]}")
        lines.append("")

    lines.append("*Use `handoff(project)` for a full briefing on any project.*")
    briefing = "\n".join(lines)

    return {
        "active_projects": len(summaries),
        "token_estimate": len(briefing) // 4,
        "briefing": briefing,
        "projects": summaries,
    }


# ===========================================================================
# Auto-observe — passive tool-call capture
# ===========================================================================

@mcp.tool()
def observe_control(
    action: str = "status",
    project: Optional[str] = None,
    method: str = "rule",
    compress_every: int = 20,
) -> dict:
    """[COMMON] Control passive tool-call capture (auto-observe) for this session.

    Merges enable/disable/status into one tool — no need to remember two names.

    Actions:
      "on"     — start capturing every tool call as an observation; daemon
                 compresses batches into checkpoints automatically
      "off"    — stop capturing for this session
      "status" — show current state + observation counts (default)

    Compression methods (only relevant for action="on"):
      rule   — fast, no API, groups tool calls by type (default)
      claude — calls Claude Haiku to write structured findings
               (requires ANTHROPIC_API_KEY)

    Tip: use auto_mode(enabled=True, project=...) to activate this alongside
    auto-sync and smart retrieval all at once.

    Args:
        action:        "on", "off", or "status"
        project:       Project to attribute observations to (required for "on")
        method:        "rule" or "claude"
        compress_every: Compress after this many observations
    """
    global _auto_observe, _observe_project, _observe_method

    if action == "on":
        _auto_observe = True
        if project:
            _observe_project = project
        _observe_method = method
    elif action == "off":
        _auto_observe = False

    all_stats  = _db.observation_stats()
    proj_stats = _db.observation_stats(_observe_project) if _observe_project else None

    return {
        "action": action,
        "auto_observe": _auto_observe,
        "observe_project": _observe_project,
        "method": _observe_method,
        "compress_every": compress_every,
        "stats": {"global": all_stats, "current_project": proj_stats},
        "message": (
            f"Auto-observe {'enabled' if _auto_observe else 'disabled'}"
            + (f" for '{_observe_project}'" if _observe_project else "")
            + (f" (method={_observe_method}, compress every {compress_every})" if _auto_observe else "")
        ),
        "tip": (
            "Call observe_control('on', project='myapp') to start capturing."
            if not _auto_observe else
            f"Capturing tool calls for '{_observe_project}'. Daemon compresses every {compress_every} observations."
        ),
    }


# Keep legacy names as thin aliases so existing agents don't break
@mcp.tool()
def auto_observe_toggle(
    enabled: bool,
    project: Optional[str] = None,
    method: str = "rule",
    compress_every: int = 20,
) -> dict:
    """[ADVANCED] Deprecated alias — use observe_control(action='on'/'off') instead."""
    return observe_control(
        action="on" if enabled else "off",
        project=project,
        method=method,
        compress_every=compress_every,
    )


@mcp.tool()
def observe_status() -> dict:
    """[ADVANCED] Deprecated alias — use observe_control(action='status') instead."""
    return observe_control(action="status")


# ===========================================================================
# Auto-mode — activate all three automation layers in one call
# ===========================================================================

@mcp.tool()
def auto_mode(
    enabled: bool,
    project: str,
    method: str = "rule",
    compress_every: int = 20,
) -> dict:
    """[CORE] Activate (or deactivate) all three Continuum automation layers at once.

    One call replaces three separate setup steps:
      Layer 1 — Auto-observe: every tool call silently captured as an observation
      Layer 2 — Auto-sync:    MEMORY.md / DECISIONS.md / TASKS.md written on every checkpoint
      Layer 3 — Smart context: handoff() and remember() auto-inject relevant history

    When enabled, just work normally — Continuum captures, compresses, and syncs
    everything in the background without any manual calls.

    Args:
        enabled: Turn all layers on or off
        project: Project to attribute observations and syncs to
        method: Observation compression method — "rule" (fast) or "claude" (Haiku)
        compress_every: Compress observations into a checkpoint after this many calls
    """
    global _auto_observe, _observe_project, _observe_method, _auto_mode_active

    _auto_mode_active = enabled
    _auto_observe     = enabled
    _observe_method   = method
    if enabled:
        _observe_project = project

    if enabled:
        # Ensure project config has auto_sync on
        from .models import ProjectConfig
        config = _db.get_project_config(project)
        if not config:
            _db.save_project_config(ProjectConfig(project=project, auto_sync=True))
        elif not config.auto_sync:
            _db.save_project_config(ProjectConfig(
                project=project,
                output_dir=config.output_dir,
                auto_sync=True,
            ))

    layers = {
        "auto_observe": enabled,
        "auto_sync": enabled,
        "smart_retrieval": enabled,
    }

    return {
        "auto_mode": enabled,
        "project": project,
        "layers": layers,
        "observe_method": method,
        "compress_every": compress_every,
        "sync_path": str(Path.home() / ".continuum" / "projects" / project),
        "message": (
            f"All automation layers {'activated' if enabled else 'deactivated'} for '{project}'. "
            + ("Just work normally — Continuum handles the rest." if enabled else "")
        ),
        "tip": (
            "Call smart_resume('" + project + "') at the start of a new session to reload context."
            if enabled else
            "Use auto_mode(enabled=True, project=...) to re-activate."
        ),
    }


# ===========================================================================
# Smart resume — 3-tier retrieval with automatic depth selection
# ===========================================================================

@mcp.tool()
def smart_resume(
    project: str,
    query: Optional[str] = None,
    window: int = 5,
) -> dict:
    """[CORE] Three-tier memory retrieval in one call — automatically picks the right depth.

    Replaces the manual memory_search → memory_timeline → memory_get workflow.
    Automatically selects how deep to go based on what it finds:

      0 results    → "no memory" message, suggests starting fresh
      1–3 results  → auto-expands to full checkpoint detail (tier 3)
      4–10 results → timeline slice around the top hit + full detail for top 2
      10+ results  → compact summaries only, with drill-down tips

    The query defaults to the project name if not specified — useful for a
    general "what was I doing?" resume at session start.

    Args:
        project: Project to retrieve context for
        query: Search terms (default: project name)
        window: Timeline window size for medium-density results
    """
    search_query = query or project

    # Tier 1 — search
    hits = _db.search_checkpoints(search_query, limit=15)
    count = len(hits)

    if count == 0:
        # Nothing in memory — fall back to latest checkpoint if exists
        cp = _db.latest_checkpoint(project)
        if not cp:
            return {
                "project": project,
                "depth": "none",
                "message": f"No memory for '{project}'. Call checkpoint() to start tracking.",
                "handoff_tip": f"Use checkpoint() to save your first state for '{project}'.",
            }
        # Return just the latest
        return {
            "project": project,
            "depth": "latest_only",
            "checkpoint": {
                "id": cp.id[:8],
                "task": cp.current_task,
                "goal": cp.goal,
                "status": cp.status,
                "findings": cp.findings,
                "next_steps": cp.next_steps,
                "dead_ends": cp.dead_ends,
                "timestamp": cp.timestamp.isoformat(),
            },
            "tip": "No FTS matches found. Showing latest checkpoint instead.",
        }

    if count <= 3:
        # Tier 3 — full detail on all hits
        ids = [h["id"] for h in hits]
        cps = _db.get_checkpoints_by_ids(ids)
        return {
            "project": project,
            "depth": "full",
            "reason": f"{count} result(s) — expanded to full detail automatically",
            "checkpoints": [
                {
                    "id": cp.id[:8],
                    "task": cp.current_task,
                    "goal": cp.goal,
                    "status": cp.status,
                    "context": cp.context,
                    "findings": cp.findings,
                    "dead_ends": cp.dead_ends,
                    "next_steps": cp.next_steps,
                    "open_questions": cp.open_questions,
                    "decisions": [d.model_dump() for d in cp.decisions],
                    "timestamp": cp.timestamp.isoformat(),
                }
                for cp in cps
            ],
        }

    if count <= 10:
        # Tier 2 — timeline around top hit + full detail for top 2
        top_id = hits[0]["id"]
        timeline = _db.checkpoint_timeline(top_id, window=window)
        top_cps = _db.get_checkpoints_by_ids([h["id"] for h in hits[:2]])
        return {
            "project": project,
            "depth": "timeline+detail",
            "reason": f"{count} results — showing timeline + full detail for top 2",
            "top_checkpoints": [
                {
                    "id": cp.id[:8],
                    "task": cp.current_task,
                    "goal": cp.goal,
                    "status": cp.status,
                    "findings": cp.findings,
                    "dead_ends": cp.dead_ends,
                    "next_steps": cp.next_steps,
                    "timestamp": cp.timestamp.isoformat(),
                }
                for cp in top_cps
            ],
            "timeline": [
                {
                    "id": cp.id[:8],
                    "task": cp.current_task,
                    "status": cp.status,
                    "timestamp": cp.timestamp.isoformat(),
                    "is_pivot": cp.id == top_id,
                }
                for cp in timeline
            ],
            "tip": f"Use memory_get(ids=[...]) for full detail on any timeline entry.",
        }

    # Tier 1 only — too many results, return compact summaries
    return {
        "project": project,
        "depth": "search_only",
        "reason": f"{count} results — showing compact summaries to save context",
        "matches": hits[:10],
        "tip": (
            "Narrow your query or use memory_get(ids=[...]) for specific entries. "
            "Top match: " + hits[0]["task"]
        ),
    }


# ===========================================================================
# Notifications — Telegram, Discord, Slack, webhook
# ===========================================================================

@mcp.tool()
def notify_configure(
    project: str,
    telegram_token:   Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    discord_webhook:  Optional[str] = None,
    slack_webhook:    Optional[str] = None,
    webhook_url:      Optional[str] = None,
    errors_only: bool = False,
) -> dict:
    """Configure notification channels for a project.

    Stored in the DB — every task that completes for this project will fire
    a message to the configured channels automatically.  No code changes needed.

    Supports:
      Telegram — get a bot token from @BotFather, then get your chat_id from
                 @userinfobot.  Messages arrive as formatted Markdown.
      Discord  — create an Incoming Webhook in Server Settings → Integrations.
      Slack    — create an Incoming Webhook app at api.slack.com/apps.
      Generic  — any URL that accepts a POST with {title, message, task_id, exit_code}.

    Args:
        project:          Project to configure notifications for
        telegram_token:   Telegram bot token (from @BotFather)
        telegram_chat_id: Telegram chat ID (from @userinfobot)
        discord_webhook:  Discord incoming webhook URL
        slack_webhook:    Slack incoming webhook URL
        webhook_url:      Generic webhook URL
        errors_only:      Only fire on task failure / non-zero exit code
    """
    import json
    from .notify import NotifyConfig

    cfg = NotifyConfig(
        telegram_token   = telegram_token,
        telegram_chat_id = telegram_chat_id,
        discord_webhook  = discord_webhook,
        slack_webhook    = slack_webhook,
        webhook_url      = webhook_url,
        errors_only      = errors_only,
    )
    _db.save_notify_config(project, json.dumps({
        "telegram_token":   telegram_token,
        "telegram_chat_id": telegram_chat_id,
        "discord_webhook":  discord_webhook,
        "slack_webhook":    slack_webhook,
        "webhook_url":      webhook_url,
        "errors_only":      errors_only,
    }))

    channels = []
    if telegram_token and telegram_chat_id: channels.append("telegram")
    if discord_webhook:  channels.append("discord")
    if slack_webhook:    channels.append("slack")
    if webhook_url:      channels.append("webhook")

    return {
        "project": project,
        "channels_configured": channels,
        "errors_only": errors_only,
        "message": (
            f"Notifications configured for '{project}' via {', '.join(channels)}. "
            "All future task completions will fire automatically."
            if channels else
            "No channels configured — pass at least one of telegram_token+telegram_chat_id, "
            "discord_webhook, slack_webhook, or webhook_url."
        ),
        "tip": "Use notify_test(project) to verify channels are working.",
    }


@mcp.tool()
def notify_when_complete(
    task_id: str,
    telegram_token:   Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    discord_webhook:  Optional[str] = None,
    slack_webhook:    Optional[str] = None,
    webhook_url:      Optional[str] = None,
    errors_only: bool = False,
) -> dict:
    """Register a one-time notification for when a specific task completes.

    Use this when you want to be alerted about a single task without configuring
    project-level notifications.  The config is automatically deleted after firing.

    Perfect for: pushing a long training job and wanting a phone message when done,
    without affecting other tasks.

    Args:
        task_id:          Task ID returned by forge_push
        telegram_token:   Telegram bot token
        telegram_chat_id: Telegram chat ID
        discord_webhook:  Discord webhook URL
        slack_webhook:    Slack webhook URL
        webhook_url:      Generic webhook URL
        errors_only:      Only fire on failure
    """
    import json

    _db.save_task_notify(task_id, json.dumps({
        "telegram_token":   telegram_token,
        "telegram_chat_id": telegram_chat_id,
        "discord_webhook":  discord_webhook,
        "slack_webhook":    slack_webhook,
        "webhook_url":      webhook_url,
        "errors_only":      errors_only,
    }))

    channels = []
    if telegram_token and telegram_chat_id: channels.append("telegram")
    if discord_webhook:  channels.append("discord")
    if slack_webhook:    channels.append("slack")
    if webhook_url:      channels.append("webhook")

    return {
        "task_id": task_id,
        "channels": channels,
        "errors_only": errors_only,
        "message": (
            f"Will notify via {', '.join(channels)} when task {task_id[:8]} completes."
            if channels else
            "No channels specified — notification will not fire."
        ),
    }


@mcp.tool()
def notify_test(
    project: str,
    message: str = "Continuum notification test — channels are working.",
) -> dict:
    """Fire a test notification to verify channel config for a project.

    Uses the project's saved config from notify_configure().  If no project
    config exists, falls back to environment variables.

    Args:
        project: Project whose config to test
        message: Custom message body (optional)
    """
    import json
    from .notify import NotifyConfig, notify

    config_json = _db.get_notify_config(project)
    if config_json:
        raw = json.loads(config_json)
        cfg = NotifyConfig(**raw)
    else:
        cfg = NotifyConfig.from_env()

    if not cfg.has_any_channel():
        return {
            "project": project,
            "sent": False,
            "message": "No channels configured. Use notify_configure() or set env vars.",
            "env_vars": [
                "CONTINUUM_TG_TOKEN + CONTINUUM_TG_CHAT",
                "CONTINUUM_DISCORD_WEBHOOK",
                "CONTINUUM_SLACK_WEBHOOK",
                "CONTINUUM_WEBHOOK_URL",
            ],
        }

    result = notify(
        title=f"[{project}] Test notification",
        message=message,
        config=cfg,
        extra={"project": project, "test": True},
    )
    return {
        "project": project,
        "sent": result.any_sent,
        "fired": result.fired,
        "failed": result.failed,
        "skipped": result.skipped,
        "summary": result.summary(),
    }


# ===========================================================================
# Personal Memory — user identity + agent behavioral protocols
# ===========================================================================


@mcp.tool()
def remember_me(
    key: str,
    value: str,
    category: str = "preferences",
    tags: Optional[List[str]] = None,
) -> dict:
    """[CORE] Store a fact about the user — persists forever across sessions.

    This is YOUR memory about the person you work with. Use it to remember
    their bio, how they like things done, technical preferences, rules they've
    set, and research interests. Recalled automatically in north_star().

    Categories: bio | preferences | technical | research | rules | relationship

    Examples:
        remember_me("name", "Jane Smith", category="bio")
        remember_me("code_style", "prefers TypeScript, clean architecture", category="technical")
        remember_me("always_explain_why", "explain every architectural decision", category="rules")

    Args:
        key: Unique slug identifier (e.g. "preferred_language")
        value: The memory content
        category: Memory category — bio/preferences/technical/research/rules/relationship
        tags: Optional list of searchable tags
    """
    from .models import UserMemory, MemoryCategory
    try:
        cat = MemoryCategory(category)
    except ValueError:
        cat = MemoryCategory.PREFERENCES
    mem = UserMemory(key=key, value=value, category=cat, tags=tags or [])
    _db.remember_user(mem)
    _maybe_observe("remember_me", {"key": key, "category": category}, {"stored": True})
    return {"stored": True, "key": key, "category": cat.value, "id": mem.id}


@mcp.tool()
def recall_me(
    query: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """[CORE] Retrieve facts about the user via full-text search or category filter.

    Use this to look up specific preferences, rules, or biographical details
    before making decisions. Works across both local Claude Code sessions and
    Claude.ai web sessions.

    Args:
        query: Search terms (FTS5, supports Porter stemming)
        category: Filter by category — bio/preferences/technical/research/rules/relationship
        limit: Maximum results to return
    """
    results = _db.recall_user(query=query, category=category, limit=limit)
    return {
        "results": results,
        "count": len(results),
        "query": query,
        "category": category,
    }


@mcp.tool()
def remember_this(
    key: str,
    value: str,
    category: str = "protocol",
    rationale: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> dict:
    """[CORE] Store a behavioral protocol or constraint for Claude.

    This is YOUR self-knowledge — how you should behave, decisions you've made
    about your approach, constraints you're operating under, and the "why"
    behind your architectural choices. Persists across context resets.

    Categories: protocol | constraint | decision | workflow | persona

    Examples:
        remember_this("explain_decisions", "always explain the why behind choices",
                      category="protocol", rationale="Zack is non-technical but wants understanding")
        remember_this("no_cloud", "keep all data local", category="constraint")
        remember_this("tone", "collaborative partner, not a tool", category="persona")

    Args:
        key: Unique slug identifier
        value: The protocol or constraint
        category: Memory category — protocol/constraint/decision/workflow/persona
        rationale: The reason behind this behavior (survives context resets)
        tags: Optional searchable tags
    """
    from .models import AgentMemory, AgentMemoryCategory
    try:
        cat = AgentMemoryCategory(category)
    except ValueError:
        cat = AgentMemoryCategory.PROTOCOL
    mem = AgentMemory(
        key=key, value=value, category=cat,
        rationale=rationale, tags=tags or []
    )
    _db.remember_agent(mem)
    _maybe_observe("remember_this", {"key": key, "category": category}, {"stored": True})
    return {"stored": True, "key": key, "category": cat.value, "id": mem.id}


@mcp.tool()
def recall_this(
    query: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """[CORE] Retrieve your behavioral protocols and constraints via search.

    Use this to recall how you should operate before starting work — your
    standing rules, constraints, and the reasoning behind past decisions.

    Args:
        query: Search terms (FTS5)
        category: Filter by — protocol/constraint/decision/workflow/persona
        limit: Maximum results
    """
    results = _db.recall_agent(query=query, category=category, limit=limit)
    return {
        "results": results,
        "count": len(results),
        "query": query,
        "category": category,
    }


@mcp.tool()
def forget(key: str, memory_type: str = "user") -> dict:
    """[COMMON] Delete a memory permanently.

    Args:
        key: The memory key to delete
        memory_type: "user" (facts about the user) or "agent" (your protocols)
    """
    if memory_type == "agent":
        deleted = _db.delete_agent_memory(key)
    else:
        deleted = _db.delete_user_memory(key)
    return {"deleted": deleted, "key": key, "type": memory_type}


@mcp.tool()
def north_star() -> dict:
    """[CORE] Your perfect session start — unified <500 token briefing.

    Returns a single structured briefing covering everything you need to
    operate effectively:

      ## You        — who the user is, their rules and preferences
      ## How I Work — your behavioral protocols and constraints
      ## Active Projects — what's in progress (last 14 days)
      ## Right Now  — most recent checkpoint state

    Call this ONCE at the start of every new session. Updates automatically
    as you add memories and checkpoints. Works identically on Claude Code
    and Claude.ai web (via Custom Connectors).

    Pair with: remember_me() and remember_this() to teach the system more.
    """
    lines: list[str] = []

    from datetime import timezone as _tz
    _now = __import__("datetime").datetime.now(_tz.utc)

    def _age(ts) -> str:
        try:
            from datetime import datetime as _dt, timezone as _tz2
            if isinstance(ts, str):
                ts = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz2.utc)
            d = (_now - ts).days
            if d == 0: return "today"
            if d == 1: return "yesterday"
            if d < 7: return f"{d}d ago"
            return ts.strftime("%Y-%m-%d")
        except Exception:
            return ""

    # ---- Section 1: User identity (~100 token budget) ----
    user_mem = _db.all_user_memory()
    if user_mem:
        lines.append("## You")
        for cat in ["bio", "rules", "preferences", "technical", "research", "relationship"]:
            entries = [m for m in user_mem if m.category == cat]
            for m in entries[:3]:
                age = _age(m.updated_at) if hasattr(m, "updated_at") and m.updated_at else ""
                age_note = f" *(updated: {age})*" if age else ""
                lines.append(f"- **{m.key}**: {m.value}{age_note}")

    # ---- Section 2: Agent protocols (~100 token budget) ----
    agent_mem = _db.all_agent_memory()
    if agent_mem:
        lines.append("\n## How I Work")
        for cat in ["protocol", "constraint", "persona", "workflow", "decision"]:
            entries = [m for m in agent_mem if m.category == cat]
            for m in entries[:3]:
                rationale_note = f" *(why: {m.rationale})*" if m.rationale else ""
                age = _age(m.updated_at) if hasattr(m, "updated_at") and m.updated_at else ""
                age_note = f" *(updated: {age})*" if age else ""
                lines.append(f"- **{m.key}**: {m.value}{rationale_note}{age_note}")

    # ---- Section 3: Active projects (~200 token budget) ----
    try:
        summaries = _db.recent_project_summaries(days=14)
    except Exception:
        summaries = []
    if summaries:
        lines.append("\n## Active Projects")
        for s in summaries[:5]:
            lines.append(
                f"- **{s['project']}**: {s.get('current_task', 'no task')} "
                f"({s.get('status', 'unknown')})"
            )

    # ---- Section 4: Most recent checkpoint (~100 token budget) ----
    try:
        all_projects = _db.list_projects()
        if all_projects:
            cp = _db.latest_checkpoint(all_projects[0])
            if cp:
                lines.append("\n## Right Now")
                lines.append(f"**{cp.project}** — {cp.current_task}")
                if cp.goal:
                    lines.append(f"Goal: {cp.goal}")
                if cp.next_steps:
                    lines.append(f"Next: {cp.next_steps[0]}")
                if cp.dead_ends:
                    lines.append(f"Avoid: {cp.dead_ends[0]}")
    except Exception:
        pass

    briefing = "\n".join(lines)
    token_estimate = len(briefing) // 4

    # Hard cap — trim if over budget
    if token_estimate > 500:
        briefing = briefing[:2000] + "\n*(truncated for token budget — use recall_me/recall_this for details)*"
        token_estimate = len(briefing) // 4

    if not briefing.strip():
        briefing = "No memory stored yet. Use remember_me() and remember_this() to build your North Star."

    return {
        "briefing": briefing,
        "token_estimate": token_estimate,
        "user_memory_count": len(user_mem),
        "agent_memory_count": len(agent_mem),
        "project_count": len(summaries) if summaries else 0,
        "tip": (
            "Build your North Star: remember_me('name', 'your name', category='bio') "
            "and remember_this('your_protocol', 'how you work', category='protocol')"
            if not user_mem and not agent_mem else
            "North Star is active. Add more with remember_me() and remember_this()."
        ),
    }


# ===========================================================================
# Sync Files — write MEMORY.md / DECISIONS.md / TASKS.md to project dir
# ===========================================================================

@mcp.tool()
def sync_files(
    project: str,
    output_dir: Optional[str] = None,
    save_dir: bool = True,
) -> dict:
    """Write MEMORY.md, DECISIONS.md, TASKS.md for a project.

    Generates three git-friendly markdown files from the project's latest
    checkpoint and history. Files are stable-formatted for version control —
    diffable, human-readable, never need manual editing.

    If output_dir is provided (and save_dir=True), it's saved as the
    configured directory for this project so future checkpoint() and handoff()
    calls sync automatically.

    If output_dir is omitted, uses the previously configured dir, or falls
    back to ~/.continuum/projects/{project}/.

    Args:
        project: Project identifier
        output_dir: Path to write files (saves as project default if save_dir=True)
        save_dir: Persist output_dir as the project's configured sync target
    """
    from .sync_files import sync_project_files
    from .models import ProjectConfig

    cp = _db.latest_checkpoint(project)
    if not cp:
        return {"error": f"No checkpoints found for project '{project}'"}

    # Resolve output directory
    config = _db.get_project_config(project)
    if output_dir:
        out_path = Path(output_dir).expanduser()
        if save_dir:
            new_config = ProjectConfig(
                project=project,
                output_dir=str(out_path),
                auto_sync=config.auto_sync if config else True,
            )
            _db.save_project_config(new_config)
    elif config and config.output_dir:
        out_path = Path(config.output_dir)
    else:
        out_path = Path.home() / ".continuum" / "projects" / project

    history = _db.list_checkpoints(project=project, limit=25)
    written = sync_project_files(cp, history, out_path)

    return {
        "project": project,
        "output_dir": str(out_path),
        "files_written": [str(p) for p in written.values()],
        "checkpoint_id": cp.id[:8],
        "tip": f"Files auto-update on every checkpoint() and handoff() for '{project}'.",
    }


# ===========================================================================
# Web ↔ Code bridge — portable handoff markdown export/import
# ===========================================================================

_HANDOFF_FENCE = "continuum-handoff: v1"


def _system_notify(title: str, message: str) -> None:
    """Fire a system notification with fallback to alerts log."""
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="Continuum", timeout=10)
    except Exception:
        pass
    try:
        alerts_path = Path.home() / ".continuum" / "alerts.log"
        with open(alerts_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()[:19]}] {title}: {message}\n")
    except Exception:
        pass


def _handoff_to_md(h: "Handoff", target_agent: str = "claude") -> str:
    """Render a Handoff as a self-contained markdown block with YAML frontmatter."""
    from .models import Handoff as _Handoff  # type hint only
    watch_lines = "\n".join(f"  - {w}" for w in h.watch_out_for)
    watch_yaml = f"watch_out_for:\n{watch_lines}" if h.watch_out_for else "watch_out_for: []"
    frontmatter = (
        f"---\n"
        f"{_HANDOFF_FENCE}\n"
        f"project: {h.project}\n"
        f"handoff_id: {h.id}\n"
        f"checkpoint_id: {h.checkpoint_id}\n"
        f"generated_at: {h.generated_at.isoformat()}\n"
        f"target_agent: {target_agent}\n"
        f"token_estimate: {h.token_estimate}\n"
        f"{watch_yaml}\n"
        f"---\n"
    )

    if target_agent == "gpt":
        body = (
            f"[SYSTEM CONTEXT — Continuum Handoff for {h.project}]\n\n"
            f"You are resuming work on project: {h.project}\n\n"
            f"## Situation\n\n{h.executive_summary}\n\n"
            f"## What You Need to Know\n\n{h.full_context}\n\n"
            f"## Your First Action\n\n{h.immediate_action}\n\n"
            f"## Do NOT Repeat These Dead Ends\n\n"
            + "\n".join(f"- {w}" for w in h.watch_out_for) + "\n"
        )
    elif target_agent == "gemini":
        body = (
            f"**Project context for {h.project}**\n\n"
            f"Background: {h.executive_summary}\n\n"
            f"Context: {h.full_context}\n\n"
            f"Task: {h.immediate_action}\n\n"
            f"Avoid: " + "; ".join(h.watch_out_for) + "\n"
        )
    elif target_agent == "generic":
        body = (
            f"CONTEXT: {h.executive_summary}\n\n"
            f"DETAILS: {h.full_context}\n\n"
            f"ACTION: {h.immediate_action}\n\n"
            f"AVOID: " + " | ".join(h.watch_out_for) + "\n"
        )
    else:  # claude (default)
        body = (
            f"## Executive Summary\n\n{h.executive_summary}\n\n"
            f"## Full Context\n\n{h.full_context}\n\n"
            f"## Immediate Action\n\n{h.immediate_action}\n"
        )

    return frontmatter + "\n" + body


def _md_to_handoff(md: str) -> "Handoff":
    """Parse a continuum handoff markdown block back into a Handoff object."""
    import re
    from .models import Handoff as _Handoff

    fm_match = re.search(r"^---\n(.*?)\n---\n", md, re.DOTALL)
    if not fm_match:
        raise ValueError("No frontmatter found — expected --- block at top")
    fm = fm_match.group(1)
    if _HANDOFF_FENCE not in fm:
        raise ValueError(f"Not a continuum handoff block (missing '{_HANDOFF_FENCE}')")

    def _fm_val(key: str) -> str:
        m = re.search(rf"^{key}: (.+)$", fm, re.MULTILINE)
        return m.group(1).strip() if m else ""

    # Parse watch_out_for list
    wof_match = re.search(r"watch_out_for:\n((?:  - .+\n?)*)", fm)
    watch_out_for = []
    if wof_match:
        watch_out_for = [
            line.lstrip("  - ").strip()
            for line in wof_match.group(1).strip().splitlines()
            if line.strip().startswith("- ")
        ]

    body = md[fm_match.end():]

    def _section(heading: str) -> str:
        m = re.search(rf"## {heading}\n\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
        return m.group(1).strip() if m else ""

    project     = _fm_val("project")
    checkpoint_id = _fm_val("checkpoint_id")
    generated_at_str = _fm_val("generated_at")
    token_estimate = int(_fm_val("token_estimate") or "0")

    from datetime import datetime, timezone as _tz
    try:
        generated_at = datetime.fromisoformat(generated_at_str)
    except Exception:
        generated_at = datetime.now(_tz.utc)

    return _Handoff(
        project=project,
        checkpoint_id=checkpoint_id,
        generated_at=generated_at,
        executive_summary=_section("Executive Summary"),
        full_context=_section("Full Context"),
        immediate_action=_section("Immediate Action"),
        watch_out_for=watch_out_for,
        token_estimate=token_estimate,
    )


@mcp.tool()
def export_handoff(
    project: str,
    checkpoint_id: Optional[str] = None,
    warn_tokens: int = 1500,
) -> dict:
    """Export a handoff as a portable markdown block.

    The exported block can be pasted into a web UI, GitHub issue, or shared
    doc. Use import_web_handoff() in any new session to load it back into DB
    and resume with full context.

    Includes token count preview and a warning if the block is large.

    Args:
        project: Project to export
        checkpoint_id: Specific checkpoint (default: latest)
        warn_tokens: Emit a warning if token estimate exceeds this
    """
    from .handoff import generate_handoff as _gen

    if checkpoint_id:
        cp = _db.get_checkpoint(checkpoint_id)
        if not cp:
            return {"error": f"Checkpoint not found: {checkpoint_id}"}
    else:
        cp = _db.latest_checkpoint(project)
        if not cp:
            return {"error": f"No checkpoints for project '{project}'"}

    h = _gen(cp)
    md = _handoff_to_md(h)
    token_estimate = len(md) // 4

    result: dict = {
        "project": project,
        "token_estimate": token_estimate,
        "markdown": md,
        "tip": "Paste this block anywhere. Load back with: import_web_handoff(md_string=...)",
    }
    if token_estimate > warn_tokens:
        result["token_warning"] = (
            f"This export is ~{token_estimate} tokens. "
            f"Injecting it cold uses context budget. "
            f"Consider memory_search() for targeted retrieval instead."
        )
    return result


@mcp.tool()
def import_web_handoff(md_string: str, save: bool = True) -> dict:
    """Import a handoff markdown block from a web UI, doc, or paste.

    Parses the continuum frontmatter + body, loads the handoff into the local
    DB, and returns a resume briefing — exactly as if you called handoff().

    Token count preview is always shown before the content is injected so you
    can decide whether to load it fully or use memory_search() instead.

    Args:
        md_string: The full markdown block produced by export_handoff()
        save: Persist the imported handoff into the local DB (default True)
    """
    try:
        h = _md_to_handoff(md_string)
    except ValueError as e:
        return {"error": str(e)}

    token_estimate = len(md_string) // 4
    if save:
        _db.save_handoff(h)

    result: dict = {
        "project": h.project,
        "checkpoint_id": h.checkpoint_id,
        "token_estimate": token_estimate,
        "executive_summary": h.executive_summary,
        "full_context": h.full_context,
        "immediate_action": h.immediate_action,
        "watch_out_for": h.watch_out_for,
        "imported": save,
        "tip": f"Context loaded for '{h.project}'. Call checkpoint() to start capturing new work.",
    }
    if token_estimate > 1500:
        result["token_warning"] = (
            f"Injecting ~{token_estimate} tokens. "
            "This is large — consider loading only executive_summary + immediate_action "
            "and calling memory_get() for deep context on demand."
        )
    return result


# ===========================================================================
# Claude Code dispatch — run headless Claude sessions as forge tasks
# ===========================================================================

@mcp.tool()
def dispatch_claude(
    prompt: str,
    project: str,
    name: Optional[str] = None,
    cwd: Optional[str] = None,
    auto_checkpoint: bool = True,
    priority: int = 5,
    depends_on: Optional[str] = None,
) -> dict:
    """Dispatch a headless Claude Code session as a background task.

    Uses `claude --print -p <prompt>` to run Claude autonomously while you're
    away — the output is captured, stored, and (if auto_checkpoint=True)
    parsed into a structured checkpoint on completion.

    Claude Code must be installed and on PATH. The session inherits your
    current environment including any MCP server configuration.

    Args:
        prompt: The task prompt to send to Claude
        project: Project to checkpoint results into
        name: Human-readable task name (default: first 60 chars of prompt)
        cwd: Working directory for the Claude session
        auto_checkpoint: Parse Claude's output into a checkpoint on completion
        priority: Queue priority (1=highest, 10=lowest)
        depends_on: Task ID that must complete before this dispatches
    """
    task_name = name or f"claude: {prompt[:55]}"
    # Escape the prompt for shell safety using single-quote wrapping
    safe_prompt = prompt.replace("'", "'\\''")
    command = f"claude --print -p '{safe_prompt}'"

    task = Task(
        name=task_name,
        command=command,
        cwd=cwd,
        project=project,
        auto_checkpoint=auto_checkpoint,
        priority=priority,
        depends_on=depends_on,
        tags=["claude-dispatch"],
    )
    _db.push_task(task)

    return {
        "task_id": task.id,
        "name": task_name,
        "project": project,
        "command": command,
        "auto_checkpoint": auto_checkpoint,
        "tip": (
            "Claude will run headlessly while you're away. "
            f"Resume with: handoff('{project}') when you return."
        ),
    }


# ===========================================================================
# Token guard — live usage tracking + alerts
# ===========================================================================

@mcp.tool()
def token_watch(
    used: int,
    limit: int,
    project: Optional[str] = None,
    warn_at: float = 0.8,
    critical_at: float = 0.95,
) -> dict:
    """Report token usage; auto-checkpoint + system alert when approaching the limit.

    Call this periodically during long sessions to guard against context overflow.
    At warn_at (default 80%) it saves an emergency checkpoint and fires a system
    notification so you can start a fresh session before hitting the wall.

    Args:
        used: Current tokens used in this session
        limit: Total context window size
        project: Project to auto-checkpoint on warning (optional but recommended)
        warn_at: Fraction at which to fire a warning (default 0.8 = 80%)
        critical_at: Fraction for critical alert (default 0.95 = 95%)
    """
    pct = used / limit if limit > 0 else 0
    _db.save_token_event(project, used, limit, pct)

    level = "ok"
    if pct >= critical_at:
        level = "critical"
    elif pct >= warn_at:
        level = "warning"

    result: dict = {
        "tokens_used": used,
        "tokens_limit": limit,
        "percent_used": round(pct * 100, 1),
        "status": level,
    }

    if level in ("warning", "critical"):
        msg = f"Token usage at {pct*100:.0f}% ({used:,}/{limit:,})"
        result["alert"] = msg

        if project:
            cp = Checkpoint(
                project=project,
                current_task=f"[{level.upper()}] Token budget at {pct*100:.0f}%",
                goal="[auto] Preserve state before context window fills",
                status="blocked",
                context=f"Token usage: {used:,}/{limit:,} ({pct*100:.0f}%). "
                        f"Auto-checkpoint triggered by token_watch.",
                findings=[f"Session reached {pct*100:.0f}% token capacity"],
                next_steps=[
                    "Call handoff() to generate a resume briefing",
                    "Start a fresh session and call handoff() to reload context",
                ],
                tags=[level, "token-warning"],
                agent="token-watch",
            )
            _db.save_checkpoint(cp)
            result["auto_checkpoint_id"] = cp.id[:8]

        notify_title = f"Continuum: {level.capitalize()} — {pct*100:.0f}% tokens used"
        notify_msg = f"{project or 'Session'}: {used:,}/{limit:,} tokens"
        if level == "critical":
            notify_msg += " — START NEW SESSION NOW"
        _system_notify(notify_title, notify_msg)

        result["recommended_action"] = (
            "Call handoff() immediately and start a fresh session."
            if level == "critical"
            else f"Consider calling handoff('{project}') soon and opening a new session."
            if project
            else "Consider saving a checkpoint and opening a new session."
        )

    return result


# ===========================================================================
# Cross-agent mode — portable handoffs for any agent type
# ===========================================================================

@mcp.tool()
def cross_agent_handoff(
    project: str,
    target_agent: str,
    checkpoint_id: Optional[str] = None,
    warn_tokens: int = 1500,
) -> dict:
    """Generate a handoff formatted for a specific agent type.

    Produces the same portable markdown block as export_handoff() but with
    framing and structure optimised for the target agent's prompt style.

    Formats:
      claude   — standard continuum format (default)
      gpt      — ChatGPT/OpenAI style with system message framing
      gemini   — Google Gemini style, bold headers, inline avoid list
      generic  — plain text, CONTEXT/DETAILS/ACTION/AVOID, works anywhere

    Args:
        project: Project to generate handoff for
        target_agent: One of: claude, gpt, gemini, generic
        checkpoint_id: Specific checkpoint (default: latest)
        warn_tokens: Token count threshold for a size warning
    """
    from .handoff import generate_handoff as _gen

    valid = ("claude", "gpt", "gemini", "generic")
    if target_agent not in valid:
        return {"error": f"target_agent must be one of: {', '.join(valid)}"}

    if checkpoint_id:
        cp = _db.get_checkpoint(checkpoint_id)
        if not cp:
            return {"error": f"Checkpoint not found: {checkpoint_id}"}
    else:
        cp = _db.latest_checkpoint(project)
        if not cp:
            return {"error": f"No checkpoints for project '{project}'"}

    h = _gen(cp, target_agent=target_agent)
    md = _handoff_to_md(h, target_agent=target_agent)
    token_estimate = len(md) // 4

    result: dict = {
        "project": project,
        "target_agent": target_agent,
        "token_estimate": token_estimate,
        "markdown": md,
        "tip": f"Paste this block into a {target_agent.capitalize()} session to resume instantly.",
    }
    if token_estimate > warn_tokens:
        result["token_warning"] = (
            f"~{token_estimate} tokens — consider sharing only the "
            f"executive_summary + immediate_action for a lighter load."
        )
    return result


# ===========================================================================
# Pattern learning — surface recurring decision patterns
# ===========================================================================

@mcp.tool()
def pattern_suggestions(
    project: str,
    min_frequency: int = 5,
) -> dict:
    """Return recurring decision patterns detected across this project's checkpoints.

    The observer thread silently analyzes decisions after each compression cycle.
    When a pattern appears 5+ times, it surfaces here with a suggested tag and rule.

    Use these to build consistent mental models, auto-tag future checkpoints,
    or turn repeated decisions into standing rules.

    Args:
        project: Project to analyze
        min_frequency: Minimum occurrences before a pattern is surfaced (default 5)
    """
    patterns = _db.get_patterns(project, min_frequency=min_frequency)

    if not patterns:
        # Run analysis on-demand if nothing cached yet
        from .observer import analyze_patterns
        try:
            analyze_patterns(_db, project, min_frequency=min_frequency)
            patterns = _db.get_patterns(project, min_frequency=min_frequency)
        except Exception:
            pass

    if not patterns:
        return {
            "project": project,
            "pattern_count": 0,
            "message": (
                f"No patterns yet with frequency >= {min_frequency}. "
                f"Patterns emerge after {min_frequency}+ similar decisions. "
                f"Keep checkpointing decisions and they'll surface automatically."
            ),
        }

    return {
        "project": project,
        "pattern_count": len(patterns),
        "patterns": [
            {
                "pattern": p.pattern_key,
                "frequency": p.frequency,
                "suggested_tag": f"#{p.suggested_tag}",
                "suggested_rule": p.suggested_rule,
                "examples": p.examples[:2],
                "last_seen": p.last_seen.isoformat()[:10],
            }
            for p in patterns
        ],
        "tip": "Use these patterns to tag checkpoints consistently or encode them as team rules.",
    }


if __name__ == "__main__":
    mcp.run()
