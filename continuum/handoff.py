"""Generate compact handoff documents from checkpoints."""
from __future__ import annotations
from .models import Checkpoint, Handoff


def generate_handoff(cp: Checkpoint, target_agent: str | None = None) -> Handoff:
    """
    Build a <1k token Handoff from a Checkpoint.

    Structured for cold-session resume: executive summary first,
    then exactly what the new agent needs — no more, no less.
    """
    status_phrase = {
        "in-progress": "Work is in progress",
        "blocked":     "Work is currently BLOCKED",
        "complete":    "Work is complete",
        "abandoned":   "Work was abandoned",
    }.get(cp.status, "Work state unknown")

    executive_summary = (
        f"{status_phrase} on project '{cp.project}'. "
        f"Current task: {cp.current_task}. "
        f"Goal: {cp.goal}"
    )

    sections: list[str] = []
    sections.append(f"# Continuum Handoff — {cp.project}")
    sections.append(f"**Checkpoint**: {cp.id[:8]}  |  **Agent**: {cp.agent or 'unknown'}  |  **Time**: {cp.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
    sections.append("")
    sections.append(f"## Goal\n{cp.goal}")
    sections.append("")
    sections.append(f"## Status\n`{cp.status}` — {cp.current_task}")
    sections.append("")

    if cp.context:
        sections.append(f"## Context\n{cp.context}")
        sections.append("")

    if cp.findings:
        sections.append("## What We Know")
        sections.extend(f"- {f}" for f in cp.findings)
        sections.append("")

    if cp.decisions:
        sections.append("## Key Decisions")
        for d in cp.decisions:
            sections.append(f"**{d.what}** — {d.why}")
            if d.alternatives_rejected:
                sections.append(f"  ✗ Rejected: {', '.join(d.alternatives_rejected)}")
        sections.append("")

    if cp.dead_ends:
        sections.append("## Dead Ends — Do Not Repeat")
        sections.extend(f"- {de}" for de in cp.dead_ends)
        sections.append("")

    if cp.open_questions:
        sections.append("## Open Questions")
        sections.extend(f"- {q}" for q in cp.open_questions)
        sections.append("")

    if cp.files_changed:
        sections.append("## Files In Play")
        sections.extend(f"- `{f}`" for f in cp.files_changed)
        sections.append("")

    if cp.next_steps:
        immediate_action = cp.next_steps[0]
        if len(cp.next_steps) > 1:
            sections.append("## Remaining Next Steps")
            sections.extend(f"- {s}" for s in cp.next_steps[1:])
            sections.append("")
    else:
        immediate_action = f"Resume: {cp.current_task}"

    sections.append(f"## Start Here\n**→ {immediate_action}**")

    full_context = "\n".join(sections)
    token_estimate = len(full_context) // 4  # rough 4 chars/token

    return Handoff(
        checkpoint_id=cp.id,
        project=cp.project,
        target_agent=target_agent,
        executive_summary=executive_summary,
        full_context=full_context,
        immediate_action=immediate_action,
        watch_out_for=list(cp.dead_ends),
        token_estimate=token_estimate,
    )
