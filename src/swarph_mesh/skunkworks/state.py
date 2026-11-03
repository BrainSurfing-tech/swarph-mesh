"""RunState: the single source of truth for a run, persisted at every transition."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional


class RunStatus(str, Enum):
    QUEUED = "queued"
    IDEATING = "ideating"
    RESEARCHING = "researching"
    ARCHITECTING = "architecting"
    AWAITING_COMMANDER = "awaiting_commander"
    APPROVED = "approved"
    REVISE_REQUESTED = "revise_requested"
    REJECTED = "rejected"
    ERROR = "error"


# A state file left in one of these with a dead pid means the process was killed.
RUNNING_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.IDEATING,
        RunStatus.RESEARCHING,
        RunStatus.ARCHITECTING,
    }
)


@dataclass
class RunState:
    run_id: str
    requesting_swarm: str
    persona: str
    seed: str
    card_id: int = 0
    status: RunStatus = RunStatus.QUEUED
    pid: int = 0
    jobs_used: int = 0
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    cut_queries: List[str] = field(default_factory=list)
    timestamps: Dict[str, float] = field(default_factory=dict)
    ideation: Optional[Dict[str, Any]] = None
    research: Optional[list] = None
    architecture: Optional[Dict[str, Any]] = None
    commander_verdict: Optional[str] = None
    commander_notes: Optional[str] = None
    arch_verdict_at_decision: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def new(
        cls, requesting_swarm: str, persona: str, seed: str, card_id: int = 0
    ) -> "RunState":
        run_id = f"sk_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        rs = cls(
            run_id=run_id,
            requesting_swarm=requesting_swarm,
            persona=persona,
            seed=seed,
            card_id=card_id,
            pid=os.getpid(),
        )
        rs.timestamps["created"] = time.time()
        return rs

    def transition(self, status: RunStatus) -> None:
        self.status = status
        self.timestamps[status.value] = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunState":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        kw["status"] = RunStatus(kw.get("status", "queued"))
        return cls(**kw)
