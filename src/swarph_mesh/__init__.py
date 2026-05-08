"""swarph-mesh — model-agnostic Python substrate for the swarph-mesh ecosystem.

The substrate gap the existing CLIs (``aichat`` / ``mods`` / Simon Willison's
``llm`` / ``gemini-cli`` / ``claude-cli``) leave open: none expose
mesh-gateway participation, per-caller attribution, structured-output
discipline, or the cooperative-protocol patterns the swarph encodes.

This package fills it as a pure Python library. Three repos make up
the v0.3.x architecture:

* ``swarph-mesh`` (this package)   — typed Protocol + adapters + SwarphCall
                                     + MeshClient. Pure library, no CLI.
* ``swarph-cli`` (separate repo)   — the ``swarph`` binary. Thin client
                                     on top of ``swarph-mesh``.
* ``swarph-meshlm`` (separate)     — Simon Willison ``llm`` plugin. Same
                                     mesh primitives wired into ``llm``'s
                                     plugin host instead of a standalone
                                     binary.

See the canonical PLAN at:
``https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md``

v0.0.1 ships only the typed substrate (LLMAdapter Protocol + ChatMessage +
LLMResponse + exception hierarchy). Phase 1 adds the Gemini adapter +
SwarphCall surface; subsequent phases add MeshClient, additional adapters,
``swarph onboard`` / ``swarph ratify`` (§15 of PLAN.md), and ``swarph
daemon`` built-in monitoring (§16 of PLAN.md).
"""

from __future__ import annotations

from swarph_mesh.exceptions import (
    AdapterError,
    SwarphMeshError,
    UnknownProvider,
)
from swarph_mesh.types import (
    ChatMessage,
    LLMAdapter,
    LLMResponse,
)

__version__ = "0.0.1"

__all__ = [
    "__version__",
    # types
    "ChatMessage",
    "LLMResponse",
    "LLMAdapter",
    # exceptions
    "SwarphMeshError",
    "AdapterError",
    "UnknownProvider",
]
