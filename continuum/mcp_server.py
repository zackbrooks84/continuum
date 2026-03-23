"""
Continuum MCP server — unified tool surface for Claude Code agents.

Tools exposed:
  Task Queue (push work offline):
    forge_push          — queue a shell command, optionally auto-checkpoint
    forge_status        — queue stats or specific task status
    forge_list          — list tasks by status
    forge_result        — full output of a completed task
    forge_cancel        — cancel a pending task
    forge_run_now       — run a task immediately (no daemon needed)

  Checkpoints (persist reasoning across sessions):
    checkpoint          — save work state: goal, findings, decisions, next_steps
    handoff             — generate compact resume briefing (<1k tokens)
    status              — latest checkpoint for a project
    history             — list recent checkpoints
    projects            — list all active projects

  Hybrid (push + checkpoint in one call):
    push_and_checkpoint — queue a task AND save a pre-run checkpoint together

  Memory (three-tier retrieval):
    memory_search       — FTS search, returns compact ID + summary list
    memory_timeline     — chronological context slice around a checkpoint
    memory_get          — full details for specific IDs
    remember            — auto-inject briefing for session start (~300 tokens)

  Auto-observe (passive capture):
    auto_observe_toggle — enable/disable passive tool-call capture for a project
    observe_status      — current observer state + observation stats

  Sync Files (structured project knowledge):
    sync_files          — write MEMORY.md / DECISIONS.md / TASKS.md to a directory

  Web ↔ Code bridge (portable handoff export/import):
    export_handoff      — export handoff as portable markdown block with token preview
    import_web_handoff  — parse + load a web-pasted handoff into local DB

  Claude Dispatch (run headless Claude sessions as tasks):
    dispatch_claude     — queue a headless `claude --print` session as a forge task

Run via:
    uv run --project /path/to/continuum python -m continuum.mcp_server

Environment variables:
    CONTINUUM_AUTO_OBSERVE=1        enable auto-observe globally
    CONTINUUM_OBSERVE_PROJECT=name  default project for observations
    CONTINUUM_OBSERVE_METHOD=claude use Claude Haiku to compress (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastmcp import FastMCP

from pathlib import Path
from .models import Task, TaskStatus, Checkpoint, Decision, ProjectConfig
from .db import DB
from .handoff import generate_handoff
from .daemon import daemon_status
from .runner import run_task, check_resources
from .observer import summarize_call

mcp = FastMCP("continuum", "0.1.0")
_db = DB()

# ---------------------------------------------------------------------------
# Session-level auto-observe state
# Seeded from env vars; toggled at runtime via auto_observe_toggle()
# ---------------------------------------------------------------------------
_auto_observe: bool = bool(os.environ.get("CONTINUUM_AUTO_OBSERVE"))
_observe_project: Optional[str] = os.environ.get("CONTINUUM_OBSERVE_PROJECT")
_observe_method: str = os.environ.get("CONTINUUM_OBSERVE_METHOD", "rule")


def _maybe_sync(project: str) -> None:
    """If a sync output dir is configured for this project, regenerate markdown files."""
    try:
        config = _db.get_project_config(project)
        if not config or not config.auto_sync or not config.output_dir:
            return
        from .sync_files import sync_project_files
        cp = _db.latest_checkpoint(project)
        if not cp:
            return
        history = _db.list_checkpoints(project=project, limit=25)
        sync_project_files(cp, history, Path(config.output_dir))
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
) -> dict:
    """Push a shell command onto the Continuum task queue.

    The daemon picks it up and runs it offline — you can close your session.
    Set auto_checkpoint=True with a project name to automatically save a
    structured checkpoint when the task completes.

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
    """
    task = Task(
        name=name,
        command=command,
        cwd=cwd,
        project=project,
        auto_checkpoint=auto_checkpoint,
        priority=priority,
        depends_on=depends_on,
        timeout=timeout,
        min_free_ram_mb=min_free_ram_mb,
        tags=tags or [],
    )
    _db.push_task(task)
    result = {
        "task_id": task.id,
        "name": task.name,
        "status": task.status.value,
        "auto_checkpoint": task.auto_checkpoint,
        "tip": "Start the daemon with: continuum daemon start",
    }
    _maybe_observe("forge_push", {"name": name, "command": command, "project": project}, result)
    return result


@mcp.tool()
def forge_status(task_id: Optional[str] = None) -> dict:
    """Get status of a specific task, or overall queue stats + daemon status.

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
    """Get the full stdout/stderr of a completed task.

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
    """Save a checkpoint of your current work state.

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
    _db.save_checkpoint(cp)
    result = {
        "checkpoint_id": cp.id,
        "project": cp.project,
        "saved_at": cp.timestamp.isoformat(),
        "message": f"Checkpoint saved for '{project}'. Resume with: continuum resume {project}",
    }
    _maybe_sync(project)
    _maybe_observe("checkpoint", {"project": project, "task": task, "status": status}, result)
    return result


@mcp.tool()
def handoff(
    project: str,
    checkpoint_id: Optional[str] = None,
    target_agent: Optional[str] = None,
    save: bool = True,
) -> dict:
    """Generate a compact resume briefing (<1k tokens) for a new agent session.

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
                "id": cp.id[:8],
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
    """Queue a task AND save a pre-run checkpoint in one call.

    Use this when you're about to go offline: it records your current
    reasoning state AND queues the work to run while you're gone.
    When you return, call handoff(project) to resume with full context.

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
def memory_search(query: str, limit: int = 10) -> dict:
    """Search across all checkpoints using full-text search.

    Returns a compact list of matching checkpoint IDs and short summaries —
    designed to be cheap to scan. Use memory_get() to fetch full details
    for the IDs you actually need.

    Args:
        query: Search terms (supports FTS5 syntax: quotes, AND/OR/NOT, prefix*)
        limit: Max results to return
    """
    results = _db.search_checkpoints(query, limit=limit)
    result = {
        "query": query,
        "count": len(results),
        "matches": results,
        "tip": "Use memory_get(ids=[...]) to fetch full details for specific checkpoints.",
    }
    _maybe_observe("memory_search", {"query": query}, result)
    return result


@mcp.tool()
def memory_timeline(checkpoint_id: str, window: int = 5) -> dict:
    """Return a chronological context slice around a checkpoint.

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
    """Fetch full checkpoint details for a list of IDs.

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
    """Auto-inject: compact briefing of all active projects for session start.

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
def auto_observe_toggle(
    enabled: bool,
    project: Optional[str] = None,
    method: str = "rule",
    compress_every: int = 20,
) -> dict:
    """Enable or disable passive auto-observe for this session.

    When enabled, every tool call is quietly summarized and stored as an
    observation. The daemon compressor thread rolls these into checkpoints
    automatically — no manual checkpoint() calls required.

    Compression methods:
      rule   — fast, no API, groups tool calls by type (default)
      claude — calls Claude Haiku to write structured findings
               (requires ANTHROPIC_API_KEY environment variable)

    Args:
        enabled: Turn auto-observe on or off
        project: Project to attribute observations to (required when enabling)
        method: "rule" or "claude"
        compress_every: Compress into a checkpoint after this many observations
    """
    global _auto_observe, _observe_project, _observe_method
    _auto_observe = enabled
    if project:
        _observe_project = project
    _observe_method = method

    return {
        "auto_observe": _auto_observe,
        "observe_project": _observe_project,
        "method": _observe_method,
        "compress_every": compress_every,
        "message": (
            f"Auto-observe {'enabled' if enabled else 'disabled'}"
            + (f" for project '{_observe_project}'" if _observe_project else "")
            + f" (method={method}, compress every {compress_every} observations)"
        ),
    }


@mcp.tool()
def observe_status() -> dict:
    """Check current auto-observe state and observation counts.

    Returns the session's observe config and DB stats so you can see
    how many observations are pending compression.
    """
    all_stats = _db.observation_stats()
    proj_stats = _db.observation_stats(_observe_project) if _observe_project else None

    return {
        "auto_observe": _auto_observe,
        "observe_project": _observe_project,
        "method": _observe_method,
        "stats": {
            "global": all_stats,
            "current_project": proj_stats,
        },
        "tip": (
            "Use auto_observe_toggle(enabled=True, project='myproject') to start."
            if not _auto_observe
            else f"Capturing tool calls for '{_observe_project}'. Daemon compresses every 20 observations."
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


def _handoff_to_md(h: "Handoff") -> str:
    """Render a Handoff as a self-contained markdown block with YAML frontmatter."""
    watch_lines = "\n".join(f"  - {w}" for w in h.watch_out_for)
    watch_yaml = f"watch_out_for:\n{watch_lines}" if h.watch_out_for else "watch_out_for: []"
    frontmatter = (
        f"---\n"
        f"{_HANDOFF_FENCE}\n"
        f"project: {h.project}\n"
        f"handoff_id: {h.id}\n"
        f"checkpoint_id: {h.checkpoint_id}\n"
        f"generated_at: {h.generated_at.isoformat()}\n"
        f"token_estimate: {h.token_estimate}\n"
        f"{watch_yaml}\n"
        f"---\n"
    )
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


if __name__ == "__main__":
    mcp.run()
