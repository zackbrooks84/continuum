"""Auto-sync structured project knowledge to markdown files.

Generates MEMORY.md, DECISIONS.md, TASKS.md from the latest checkpoint.
Files are git-friendly: stable format, clean markdown, version-control-safe.

Called automatically after checkpoint() and handoff() when an output
directory is configured for the project via sync_files() or the CLI.
"""
from __future__ import annotations

from pathlib import Path

from .models import Checkpoint

# HTML comment hidden from rendered markdown — marks auto-generated files
_HEADER = "<!-- continuum:auto-generated — do not edit manually -->\n"


def _ts(cp: Checkpoint) -> str:
    return cp.timestamp.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_memory(cp: Checkpoint) -> str:
    """MEMORY.md — goal, status, findings, context, open questions."""
    lines: list[str] = [
        _HEADER,
        f"# {cp.project} — Memory",
        "",
        f"> **Status:** `{cp.status}`  |  **Updated:** {_ts(cp)}",
        "",
        "## Goal",
        cp.goal,
        "",
        "## Current Task",
        f"**{cp.current_task}**",
        "",
    ]

    if cp.context:
        lines += ["## Context", cp.context, ""]

    if cp.findings:
        lines += ["## What We Know"]
        lines += [f"- {f}" for f in cp.findings]
        lines += [""]

    if cp.open_questions:
        lines += ["## Open Questions"]
        lines += [f"- {q}" for q in cp.open_questions]
        lines += [""]

    if cp.files_changed:
        lines += ["## Files In Play"]
        lines += [f"- `{f}`" for f in cp.files_changed]
        lines += [""]

    if cp.agent:
        lines += [f"---", f"*Agent: {cp.agent}  |  Checkpoint: {cp.id[:8]}*", ""]

    return "\n".join(lines)


def render_decisions(cp: Checkpoint) -> str:
    """DECISIONS.md — key decisions with reasoning + dead ends to avoid."""
    lines: list[str] = [
        _HEADER,
        f"# {cp.project} — Decisions",
        "",
        f"> **Updated:** {_ts(cp)}",
        "",
    ]

    if cp.dead_ends:
        lines += [
            "## Dead Ends — Do Not Repeat",
            "",
        ]
        lines += [f"- {de}" for de in cp.dead_ends]
        lines += [""]

    if cp.decisions:
        lines += [
            "## Key Decisions",
            "",
            "| Decision | Why | Alternatives Rejected |",
            "|----------|-----|-----------------------|",
        ]
        for d in cp.decisions:
            rejected = ", ".join(d.alternatives_rejected) if d.alternatives_rejected else "—"
            # Sanitize pipe characters in table cells
            what = d.what.replace("|", "\\|")
            why  = d.why.replace("|", "\\|")
            lines += [f"| {what} | {why} | {rejected} |"]
        lines += [""]

    if not cp.dead_ends and not cp.decisions:
        lines += ["*No decisions recorded yet.*", ""]

    return "\n".join(lines)


def render_tasks(cp: Checkpoint, history: list[Checkpoint]) -> str:
    """TASKS.md — current task, next steps checklist, checkpoint history table."""
    lines: list[str] = [
        _HEADER,
        f"# {cp.project} — Tasks",
        "",
        f"> **Updated:** {_ts(cp)}",
        "",
        "## Current Task",
        "",
        f"**{cp.current_task}** — `{cp.status}`",
        "",
    ]

    if cp.next_steps:
        lines += ["## Next Steps", ""]
        lines += [f"- [ ] {s}" for s in cp.next_steps]
        lines += [""]

    if history:
        lines += [
            "## Checkpoint History",
            "",
            "| Date | Task | Status |",
            "|------|------|--------|",
        ]
        for h in history[:25]:
            ts_short = h.timestamp.strftime("%Y-%m-%d %H:%M")
            task_cell = h.current_task[:55].replace("|", "\\|")
            lines += [f"| {ts_short} | {task_cell} | `{h.status}` |"]
        lines += [""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

def sync_project_files(
    cp: Checkpoint,
    history: list[Checkpoint],
    output_dir: Path,
) -> dict[str, Path]:
    """Write MEMORY.md, DECISIONS.md, TASKS.md to output_dir. Returns paths written."""
    output_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "MEMORY.md":    render_memory(cp),
        "DECISIONS.md": render_decisions(cp),
        "TASKS.md":     render_tasks(cp, history),
    }

    written: dict[str, Path] = {}
    for filename, content in files.items():
        path = output_dir / filename
        path.write_text(content, encoding="utf-8")
        written[filename] = path

    return written
