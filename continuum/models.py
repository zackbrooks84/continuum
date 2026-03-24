"""Unified data models for Continuum — tasks, results, checkpoints, handoffs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING   = "pending"
    WAITING   = "waiting"    # waiting on dependency
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    command: str
    cwd: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5                        # 1 (highest) to 10 (lowest)
    depends_on: Optional[str] = None
    project: Optional[str] = None            # checkpoint results here on completion
    auto_checkpoint: bool = False            # if True, parse stdout → rich checkpoint
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    timeout: Optional[int] = None
    min_free_ram_mb: int = 256
    tags: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    task_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    finished_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def success(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Checkpoint / Handoff
# ---------------------------------------------------------------------------

class Decision(BaseModel):
    """A key choice made during work, with reasoning."""
    what: str
    why: str
    alternatives_rejected: list[str] = Field(default_factory=list)


class Checkpoint(BaseModel):
    """Point-in-time snapshot of work state — survives session death."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    current_task: str
    status: Literal["in-progress", "blocked", "complete", "abandoned"] = "in-progress"
    goal: str
    context: str = ""

    findings: list[str] = Field(default_factory=list)
    dead_ends: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)

    git_ref: Optional[str] = None
    agent: Optional[str] = None
    task_id: Optional[str] = None           # forge task that produced this checkpoint
    tags: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Per-project settings — primarily the output dir for synced files."""
    project: str
    output_dir: Optional[str] = None        # path to write MEMORY/DECISIONS/TASKS.md
    auto_sync: bool = True                   # sync files automatically on checkpoint/handoff


class PatternSuggestion(BaseModel):
    """A recurring decision pattern detected across checkpoints."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    project: str
    pattern_key: str                         # normalized bigram/phrase
    frequency: int                           # times seen
    examples: list[str] = Field(default_factory=list)  # sample decision texts
    suggested_tag: str = ""                  # auto-derived tag name
    suggested_rule: str = ""                 # human-readable rule suggestion
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Handoff(BaseModel):
    """<1k token briefing optimised for new-session agent resume."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    checkpoint_id: str
    project: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    target_agent: Optional[str] = None

    executive_summary: str    # 2-3 sentences: what, where, why
    full_context: str          # everything a new session needs
    immediate_action: str      # single first thing to do
    watch_out_for: list[str]  # dead ends not to repeat
    token_estimate: int = 0


# ---------------------------------------------------------------------------
# Personal Memory — user identity + agent protocols
# ---------------------------------------------------------------------------

class MemoryCategory(str, Enum):
    BIO          = "bio"
    PREFERENCES  = "preferences"
    TECHNICAL    = "technical"
    RESEARCH     = "research"
    RULES        = "rules"
    RELATIONSHIP = "relationship"


class UserMemory(BaseModel):
    """A fact about the user — bio, preferences, rules, technical style."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    key: str           # unique slug, e.g. "preferred_language"
    value: str         # the actual memory content
    category: MemoryCategory = MemoryCategory.PREFERENCES
    source: str = "cli"   # "cli" | "web"
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentMemoryCategory(str, Enum):
    PROTOCOL   = "protocol"
    CONSTRAINT = "constraint"
    DECISION   = "decision"
    WORKFLOW   = "workflow"
    PERSONA    = "persona"


class AgentMemory(BaseModel):
    """A behavioral protocol or constraint for Claude — survives context resets."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    key: str
    value: str
    category: AgentMemoryCategory = AgentMemoryCategory.PROTOCOL
    rationale: Optional[str] = None   # the "why" behind this behavior
    source: str = "cli"
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
