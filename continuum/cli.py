"""Continuum CLI — unified command surface for task queue + checkpoints."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.text import Text

console = Console()


def _db():
    from .db import DB
    return DB()


# ===========================================================================
# Root
# ===========================================================================

@click.group()
@click.version_option()
def cli():
    """Continuum — async task queue + work continuity for AI agents.

    Push tasks to run offline. Checkpoint reasoning. Resume without context loss.

    Quick start:
      continuum setup              # create dirs, start daemon
      continuum push 'npm test'    # queue a task
      continuum resume myproject   # load briefing for new session
    """


# ===========================================================================
# Setup
# ===========================================================================

@cli.command()
def setup():
    """Create ~/.continuum dirs and start the daemon."""
    continuum_dir = Path.home() / ".continuum"
    (continuum_dir / "logs").mkdir(parents=True, exist_ok=True)
    console.print(f"[green]\u2713[/] Created {continuum_dir}")

    db = _db()
    console.print(f"[green]\u2713[/] Database ready: {db.db_path}")

    from .daemon import daemon_status, start_daemon
    s = daemon_status()
    if s["running"]:
        console.print(f"[yellow]\u26a1[/] Daemon already running (pid {s['pid']})")
    else:
        console.print("[green]\u2713[/] Starting daemon...")
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "-m", "continuum.daemon_entry"],
            stdout=open(continuum_dir / "daemon.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        console.print("[green]\u2713[/] Daemon started in background")

    console.print("\n[bold]Continuum is ready.[/] Try:")
    console.print("  continuum push 'echo hello'")
    console.print("  continuum status")


# ===========================================================================
# Task Queue
# ===========================================================================

@cli.command()
@click.argument("command")
@click.option("--name", "-n", default=None, help="Human-readable task name")
@click.option("--project", "-p", default=None, help="Project to checkpoint results into")
@click.option("--auto-checkpoint", is_flag=True, help="Auto-save checkpoint when task completes")
@click.option("--auto-observe", is_flag=True, help="Enable auto-observe for this task's project")
@click.option("--priority", default=5, show_default=True, help="1=highest, 10=lowest")
@click.option("--cwd", default=None, help="Working directory")
@click.option("--timeout", default=None, type=int, help="Max seconds to run")
@click.option("--tag", multiple=True, help="Tags (repeatable)")
@click.option("--run-now", is_flag=True, help="Run immediately instead of queuing")
def push(command, name, project, auto_checkpoint, auto_observe, priority, cwd, timeout, tag, run_now):
    """Queue a shell command to run offline.

    \b
    Examples:
      continuum push 'pytest tests/'
      continuum push 'python train.py' --project ml-run --auto-checkpoint
      continuum push 'npm run build' --priority 1 --run-now
    """
    from .models import Task, TaskStatus
    db = _db()

    task_name = name or command[:60]
    task = Task(
        name=task_name,
        command=command,
        cwd=cwd,
        project=project,
        auto_checkpoint=auto_checkpoint,
        priority=priority,
        timeout=timeout,
        tags=list(tag),
    )

    if run_now:
        from .runner import run_task, check_resources
        from datetime import datetime, timezone
        ok, reason = check_resources(task.min_free_ram_mb)
        if not ok:
            console.print(f"[red]\u2717[/] Insufficient resources: {reason}")
            sys.exit(1)
        console.print(f"[yellow]\u26a1[/] Running now: {command}")
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        db.push_task(task)
        db.update_task(task)
        from .runner import auto_checkpoint as _auto_cp
        result = run_task(task)
        task.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
        task.finished_at = datetime.now(timezone.utc)
        db.update_task(task)
        db.save_result(result)
        _auto_cp(task, result)
        icon = "[green]\u2713[/]" if result.success else "[red]\u2717[/]"
        console.print(f"{icon} Exit {result.exit_code} in {result.duration_seconds:.1f}s")
        if result.stdout:
            console.print(result.stdout[-2000:])
    else:
        db.push_task(task)
        console.print(f"[green]\u2713[/] Queued task [bold]{task.id}[/]: {task_name}")
        if project and auto_checkpoint:
            console.print(f"  Will checkpoint to project [cyan]{project}[/] on completion")
        if auto_observe and project:
            import os
            os.environ["CONTINUUM_AUTO_OBSERVE"] = "1"
            os.environ["CONTINUUM_OBSERVE_PROJECT"] = project
            console.print(f"  Auto-observe [green]on[/] for project [cyan]{project}[/]")
        console.print("  Start daemon: [dim]continuum daemon start[/]")


@cli.group()
def daemon():
    """Manage the background task runner."""


@daemon.command("start")
@click.option("--workers", default=2, show_default=True)
def daemon_start(workers):
    """Start the background daemon."""
    from .daemon import start_daemon
    start_daemon(workers=workers)


@daemon.command("stop")
def daemon_stop():
    """Stop the background daemon."""
    from .daemon import stop_daemon
    stop_daemon()


@daemon.command("status")
def daemon_status_cmd():
    """Show daemon status."""
    from .daemon import daemon_status
    s = daemon_status()
    if s["running"]:
        console.print(f"[green]\u25cf Daemon running[/] (pid {s['pid']})")
    else:
        console.print("[dim]\u25cb Daemon not running[/]")
        console.print("  Start with: continuum daemon start")


@cli.command("list")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.option("--limit", default=20, show_default=True)
def list_tasks(status, limit):
    """List tasks in the queue."""
    db = _db()
    tasks = db.list_tasks(status=status, limit=limit)
    if not tasks:
        console.print("[dim]No tasks found.[/]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Name")
    table.add_column("Status", width=10)
    table.add_column("Pri", width=3)
    table.add_column("Project", style="dim")
    table.add_column("Created", style="dim")

    status_colors = {
        "pending": "yellow", "running": "green", "done": "dim",
        "failed": "red", "cancelled": "dim", "waiting": "blue",
    }

    for t in tasks:
        color = status_colors.get(t.status.value, "white")
        table.add_row(
            t.id,
            t.name[:40],
            f"[{color}]{t.status.value}[/]",
            str(t.priority),
            t.project or "",
            t.created_at.strftime("%m/%d %H:%M"),
        )
    console.print(table)


@cli.command()
@click.argument("task_id")
def result(task_id):
    """Show the full output of a completed task."""
    db = _db()
    r = db.get_result(task_id)
    if not r:
        task = db.get_task(task_id)
        if not task:
            console.print(f"[red]Task {task_id} not found[/]")
            sys.exit(1)
        console.print(f"[yellow]Task {task_id} is {task.status.value} \u2014 no result yet[/]")
        return
    icon = "[green]\u2713[/]" if r.success else "[red]\u2717[/]"
    console.print(f"{icon} Exit {r.exit_code} | {r.duration_seconds:.1f}s | {r.finished_at.strftime('%Y-%m-%d %H:%M')}")
    if r.stdout:
        console.print(Panel(r.stdout[-3000:], title="stdout", border_style="dim"))
    if r.stderr:
        console.print(Panel(r.stderr[-1000:], title="stderr", border_style="red"))


@cli.command()
@click.argument("task_id")
def cancel(task_id):
    """Cancel a pending task."""
    db = _db()
    ok = db.cancel_task(task_id)
    if ok:
        console.print(f"[green]\u2713[/] Cancelled {task_id}")
    else:
        console.print(f"[red]\u2717[/] Could not cancel {task_id} (not pending?)")


# ===========================================================================
# Checkpoints
# ===========================================================================

@cli.command()
@click.argument("project")
@click.option("--task", "-t", required=True, help="What you're doing right now")
@click.option("--goal", "-g", required=True, help="Overall goal")
@click.option("--status", "-s", default="in-progress",
              type=click.Choice(["in-progress", "blocked", "complete", "abandoned"]))
@click.option("--context", "-c", default="", help="Background / constraints")
@click.option("--finding", multiple=True, help="Key findings (repeatable)")
@click.option("--dead-end", multiple=True, help="Dead ends to avoid (repeatable)")
@click.option("--next", multiple=True, help="Next steps (repeatable, ordered)")
@click.option("--question", multiple=True, help="Open questions (repeatable)")
@click.option("--file", "files", multiple=True, help="Files changed (repeatable)")
@click.option("--agent", default=None, help="Agent identifier")
def cp(project, task, goal, status, context, finding, dead_end, next, question, files, agent):
    """Save a checkpoint for a project.

    \b
    Example:
      continuum cp myproject \\
        --task "Refactoring auth module" \\
        --goal "Make auth testable without DB" \\
        --finding "JWT decode is tightly coupled to User model" \\
        --dead-end "Tried mock patching \u2014 breaks 12 tests" \\
        --next "Extract UserResolver interface" \\
        --next "Inject resolver in JWTMiddleware"
    """
    from .models import Checkpoint
    db = _db()
    checkpoint = Checkpoint(
        project=project,
        current_task=task,
        goal=goal,
        status=status,
        context=context,
        findings=list(finding),
        dead_ends=list(dead_end),
        next_steps=list(next),
        open_questions=list(question),
        files_changed=list(files),
        agent=agent,
    )
    db.save_checkpoint(checkpoint)
    console.print(f"[green]\u2713[/] Checkpoint saved: [cyan]{checkpoint.id[:8]}[/] for project [bold]{project}[/]")
    console.print(f"  Resume: [dim]continuum resume {project}[/]")


@cli.command()
@click.argument("project")
@click.option("--checkpoint-id", default=None, help="Specific checkpoint (default: latest)")
@click.option("--compact", is_flag=True, help="Print executive summary only")
def resume(project, checkpoint_id, compact):
    """Load a handoff briefing to resume work on a project.

    Prints a <1k token context document. Paste it at the start of a new
    agent session to resume without re-explaining anything.

    \b
    Example:
      continuum resume myproject
      continuum resume myproject --compact
    """
    db = _db()
    if checkpoint_id:
        cp_obj = db.get_checkpoint(checkpoint_id)
        if not cp_obj:
            console.print(f"[red]Checkpoint {checkpoint_id} not found[/]")
            sys.exit(1)
    else:
        cp_obj = db.latest_checkpoint(project)
        if not cp_obj:
            console.print(f"[red]No checkpoints for project '{project}'[/]")
            sys.exit(1)

    from .handoff import generate_handoff
    h = generate_handoff(cp_obj)
    db.save_handoff(h)

    if compact:
        console.print(Panel(h.executive_summary, title=f"[bold]{project}[/] \u2014 resume briefing", border_style="cyan"))
        console.print(f"\n[bold]First action:[/] {h.immediate_action}")
        if h.watch_out_for:
            console.print("\n[bold]Watch out for:[/]")
            for w in h.watch_out_for:
                console.print(f"  [red]\u2717[/] {w}")
    else:
        console.print(Panel(
            h.full_context,
            title=f"[bold]{project}[/] \u2014 resume briefing (~{h.token_estimate} tokens)",
            border_style="cyan",
        ))


@cli.command()
@click.option("--project", "-p", default=None, help="Filter by project")
@click.option("--limit", default=15, show_default=True)
def log(project, limit):
    """List recent checkpoints."""
    db = _db()
    checkpoints = db.list_checkpoints(project=project, limit=limit)
    if not checkpoints:
        console.print("[dim]No checkpoints found.[/]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Project", style="bold")
    table.add_column("Status", width=12)
    table.add_column("Task")
    table.add_column("Time", style="dim")

    status_colors = {
        "in-progress": "yellow", "blocked": "red",
        "complete": "green", "abandoned": "dim",
    }
    for cp_obj in checkpoints:
        color = status_colors.get(cp_obj.status, "white")
        table.add_row(
            cp_obj.id[:8],
            cp_obj.project,
            f"[{color}]{cp_obj.status}[/]",
            cp_obj.current_task[:45],
            cp_obj.timestamp.strftime("%m/%d %H:%M"),
        )
    console.print(table)


@cli.command()
def status():
    """Show dashboard: daemon, queue, and active projects."""
    from .daemon import daemon_status as _ds
    db = _db()

    # Daemon
    ds = _ds()
    daemon_str = "[green]\u25cf running[/]" if ds["running"] else "[dim]\u25cb stopped[/]"
    if ds.get("pid"):
        daemon_str += f" (pid {ds['pid']})"

    # Queue stats
    stats = db.task_stats()
    total = sum(stats.values())

    # Projects
    project_names = db.list_projects()

    console.print()
    console.print(f"[bold]Continuum[/]  daemon: {daemon_str}  |  tasks: {total}")
    console.print()

    # Queue table
    if total:
        qtable = Table(title="Queue", box=box.SIMPLE, show_header=True, header_style="bold dim")
        for s, n in sorted(stats.items()):
            colors = {"pending": "yellow", "running": "green", "done": "dim", "failed": "red"}
            c = colors.get(s, "white")
            qtable.add_column(s, style=c)
        qtable.add_row(*[str(stats.get(s, 0)) for s in sorted(stats.keys())])
        console.print(qtable)

    # Projects table
    if project_names:
        ptable = Table(title="Projects", box=box.SIMPLE, show_header=True, header_style="bold dim")
        ptable.add_column("Project", style="bold")
        ptable.add_column("Status", width=12)
        ptable.add_column("Latest task")
        ptable.add_column("Updated", style="dim")
        status_colors = {
            "in-progress": "yellow", "blocked": "red",
            "complete": "green", "abandoned": "dim",
        }
        for name in project_names:
            cp_obj = db.latest_checkpoint(name)
            if cp_obj:
                c = status_colors.get(cp_obj.status, "white")
                ptable.add_row(
                    name,
                    f"[{c}]{cp_obj.status}[/]",
                    cp_obj.current_task[:40],
                    cp_obj.timestamp.strftime("%m/%d %H:%M"),
                )
        console.print(ptable)

    if not total and not project_names:
        console.print("[dim]Nothing yet. Try: continuum push 'echo hello'[/]")
    console.print()


# ===========================================================================
# Observe
# ===========================================================================

@cli.group()
def observe():
    """Manage auto-observe (passive tool-call capture)."""


@observe.command("on")
@click.argument("project")
@click.option("--method", default="rule", type=click.Choice(["rule", "claude"]),
              show_default=True, help="Compression method")
@click.option("--compress-every", default=20, show_default=True,
              help="Compress after N observations")
def observe_on(project, method, compress_every):
    """Enable auto-observe for PROJECT.

    Sets CONTINUUM_AUTO_OBSERVE and CONTINUUM_OBSERVE_PROJECT in the current
    shell session. Observations are compressed into checkpoints by the daemon.

    \b
    Example:
      continuum observe on myapp --method claude
    """
    import os
    os.environ["CONTINUUM_AUTO_OBSERVE"] = "1"
    os.environ["CONTINUUM_OBSERVE_PROJECT"] = project
    os.environ["CONTINUUM_OBSERVE_METHOD"] = method
    console.print(f"[green]\u2713[/] Auto-observe [green]enabled[/] for project [cyan]{project}[/]")
    console.print(f"  Method: {method}  |  Compress every: {compress_every} observations")
    console.print(f"  Tip: export CONTINUUM_AUTO_OBSERVE=1 CONTINUUM_OBSERVE_PROJECT={project}")


@observe.command("off")
def observe_off():
    """Disable auto-observe."""
    import os
    os.environ.pop("CONTINUUM_AUTO_OBSERVE", None)
    os.environ.pop("CONTINUUM_OBSERVE_PROJECT", None)
    console.print("[dim]\u25cb Auto-observe disabled[/]")


@observe.command("status")
@click.option("--project", "-p", default=None)
def observe_status_cmd(project):
    """Show observation counts and pending compression."""
    db = _db()
    all_stats = db.observation_stats()
    console.print()
    console.print(f"[bold]Observations[/]  total: {all_stats['total']}  pending: {all_stats['pending']}")
    if project:
        ps = db.observation_stats(project)
        console.print(f"  [cyan]{project}[/]  total: {ps['total']}  pending: {ps['pending']}")
    console.print()


if __name__ == "__main__":
    cli()
