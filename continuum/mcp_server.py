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
    remember            — auto-inject briefing for session start

Run via:
    uv run --project /path/to/continuum python -m continuum.mcp_server
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .models import Task, TaskStatus, Checkpoint, Decision
from .db import DB
from .handoff import generate_handoff
from .daemon import daemon_status
from .runner import run_task, check_resources

mcp = FastMCP("continuum", "0.1.0")
_db = DB()


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
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status.value,
        "auto_checkpoint": task.auto_checkpoint,
        "tip": "Start the daemon with: continuum daemon start",
    }


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
    return {
        "checkpoint_id": cp.id,
        "project": cp.project,
        "saved_at": cp.timestamp.isoformat(),
        "message": f"Checkpoint saved for '{project}'. Resume with: continuum resume {project}",
    }


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

    return {
        "handoff_id": h.id,
        "checkpoint_id": h.checkpoint_id,
        "token_estimate": h.token_estimate,
        "executive_summary": h.executive_summary,
        "full_context": h.full_context,
        "immediate_action": h.immediate_action,
        "watch_out_for": h.watch_out_for,
    }


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

    return {
        "task_id": task.id,
        "checkpoint_id": cp.id,
        "project": project,
        "message": f"Task queued and checkpoint saved. Resume with: continuum resume {project}",
    }


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
    return {
        "query": query,
        "count": len(results),
        "matches": results,
        "tip": "Use memory_get(ids=[...]) to fetch full details for specific checkpoints.",
    }


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


if __name__ == "__main__":
    mcp.run()
