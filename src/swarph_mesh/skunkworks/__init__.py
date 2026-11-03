"""Skunkworks: ideation, web research, architect audit, human gate, on subscription lanes.

Provenance: pipeline.py, gate.py, state.py, persona.py, request.py and artifacts.py
are adapted from research/skunkworks in the private hedge-fund-mcp repo at commit
7d37186 (the source's persona pack and curvature modifier were not copied). Changes
from that copy are listed in CHANGELOG.md under 0.9.1.

Nothing here makes a metered API call. Ideation and the architect run as
``subscription_chat`` jobs on the mesh queue; research runs a local ``claude -p``
with web tools only. There is no daemon: a killed process leaves the run in ERROR.
"""

from .ceilings import RunRefused, session_blocked
from .gate import GateRefusal, check_architect_verdict_gate
from .persona import PersonaPack, load_persona
from .pipeline import Pipeline, PipelineNode
from .request import SkunkworksRequest
from .state import RunState, RunStatus

__all__ = [
    "GateRefusal",
    "Pipeline",
    "PipelineNode",
    "PersonaPack",
    "RunRefused",
    "RunState",
    "RunStatus",
    "SkunkworksRequest",
    "check_architect_verdict_gate",
    "load_persona",
    "session_blocked",
]
