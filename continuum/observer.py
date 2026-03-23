"""Auto-observe: passive semantic capture of agent tool activity.

When enabled, every MCP tool call is quietly summarized and stored as an observation.
A background thread periodically compresses batches into full checkpoints.

Compression methods:
  rule    — fast, no API needed, groups by tool type and extracts activity patterns
  claude  — calls Claude Haiku in a side-thread for structured findings
              (requires ANTHROPIC_API_KEY env var or CONTINUUM_CLAUDE_KEY)
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

_COMPRESS_INTERVAL = 60   # seconds between background compression sweeps


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def summarize_call(tool_name: str, kwargs: dict, result: Any) -> str:
    """Generate a short human-readable summary of a single tool call."""

    def _trunc(v: Any, n: int = 80) -> str:
        s = str(v)
        return s[:n] + "\u2026" if len(s) > n else s

    # Pick up to 3 meaningful args (skip large lists / None / empty)
    key_args = {
        k: _trunc(v)
        for k, v in kwargs.items()
        if v is not None and v != [] and v != {} and k not in ("ids",)
    }
    args_str = ", ".join(f"{k}={v}" for k, v in list(key_args.items())[:3])

    # Extract one meaningful value from result
    result_note = ""
    if isinstance(result, dict):
        for key in ("task_id", "checkpoint_id", "project", "status",
                    "count", "active_projects", "error", "message"):
            if key in result:
                result_note = f" \u2192 {key}={_trunc(result[key])}"
                break

    return f"{tool_name}({args_str}){result_note}"


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

def compress_rule(observations: list[dict]) -> dict:
    """Rule-based: group by tool, keep last summary per tool + call counts."""
    by_tool: dict[str, list[str]] = {}
    for obs in observations:
        by_tool.setdefault(obs["tool_name"], []).append(obs["summary"])

    findings = []
    for tool, summaries in by_tool.items():
        if len(summaries) == 1:
            findings.append(summaries[0])
        else:
            findings.append(f"{summaries[-1]}  (+{len(summaries) - 1} earlier {tool} calls)")

    start = observations[0]["timestamp"][:16]
    end   = observations[-1]["timestamp"][:16]
    context = f"Auto-captured {len(observations)} tool calls ({start} \u2192 {end})"

    return {"findings": findings[:10], "context": context, "next_steps": []}


def compress_claude(observations: list[dict], model: str, api_key: Optional[str]) -> dict:
    """Use Claude Haiku in a side-thread to compress observations into structured findings."""
    try:
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CONTINUUM_CLAUDE_KEY")
        client = anthropic.Anthropic(api_key=key)

        obs_text = "\n".join(
            f"  [{o['timestamp'][11:16]}] {o['tool_name']}: {o['summary']}"
            for o in observations
        )

        msg = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    "Compress these agent tool-call observations into a structured work checkpoint.\n\n"
                    f"Observations:\n{obs_text}\n\n"
                    "Return JSON only (no markdown) with these exact keys:\n"
                    "  findings: list[str]   \u2014 key facts/outcomes, max 8, \u226480 chars each\n"
                    "  next_steps: list[str] \u2014 logical next actions, max 3\n"
                    "  context: str          \u2014 one sentence: what was accomplished"
                ),
            }],
        )

        import json
        return json.loads(msg.content[0].text)

    except Exception:
        return compress_rule(observations)   # always fall back to rule-based


# ---------------------------------------------------------------------------
# Background compressor thread
# ---------------------------------------------------------------------------

class ObserverThread:
    """Background thread that compresses accumulated observations into checkpoints."""

    def __init__(
        self,
        db,
        compress_every: int = 20,
        method: str = "rule",
        claude_model: str = "claude-haiku-4-5-20251001",
        interval: int = _COMPRESS_INTERVAL,
    ):
        self.db = db
        self.compress_every = compress_every
        self.method = method
        self.claude_model = claude_model
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="continuum-observer"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._compress_pending()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def _compress_pending(self) -> None:
        projects = self.db.projects_needing_compression(self.compress_every)
        for project in projects:
            observations = self.db.get_uncompressed_observations(
                project, limit=self.compress_every * 2
            )
            if len(observations) < self.compress_every:
                continue

            batch = observations[: self.compress_every]

            if self.method == "claude":
                compressed = compress_claude(batch, self.claude_model, None)
            else:
                compressed = compress_rule(batch)

            from .models import Checkpoint

            cp = Checkpoint(
                project=project,
                current_task="Auto-observed session activity",
                goal=f"[auto-observe] {project}",
                status="in-progress",
                context=compressed.get("context", ""),
                findings=compressed.get("findings", []),
                next_steps=compressed.get("next_steps", []),
                agent="continuum-observer",
            )
            self.db.save_checkpoint(cp)
            self.db.mark_observations_compressed([o["id"] for o in batch])
