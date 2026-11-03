"""Run artifacts under ``<state_root>/<cell>/skunkworks/<run_id>/``.

request, state, ideation, research, architecture and assessment, all JSON, written
atomically. Nothing here writes code or touches a repo: approval produces no code.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from .state import RunState

RUN_FILES = {
    "ideation": "ideation.json",
    "research": "research.json",
    "architecture": "architecture.json",
}


def run_dir(runs_dir: str, run_id: str) -> str:
    return os.path.join(runs_dir, run_id)


def write_json(path: str, obj: Any) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def persist(state: RunState, runs_dir: str) -> None:
    """Write state.json and whichever stage outputs exist so far."""
    d = run_dir(runs_dir, state.run_id)
    write_json(os.path.join(d, "state.json"), state.to_dict())
    for attr, name in RUN_FILES.items():
        val = getattr(state, attr)
        if val is not None:
            write_json(os.path.join(d, name), val)


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_assessment_always(state: RunState, runs_dir: str) -> str:
    """assessment.json on every commander decision, whatever the verdict."""
    arch = state.architecture or {}
    return write_json(
        os.path.join(run_dir(runs_dir, state.run_id), "assessment.json"),
        {
            "run_id": state.run_id,
            "architect_verdict": state.arch_verdict_at_decision,
            "audit": arch.get("audit"),
            "mechanism": arch.get("mechanism"),
            "commander_verdict": state.commander_verdict,
            "commander_words": state.commander_notes,
        },
    )
