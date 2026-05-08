"""swarph-mesh — model-agnostic Python substrate for the swarph-mesh ecosystem.

The substrate gap the existing CLIs (``aichat`` / ``mods`` / Simon Willison's
``llm`` / ``gemini-cli`` / ``claude-cli``) leave open: none expose
mesh-gateway participation, per-caller attribution, structured-output
discipline, or the cooperative-protocol patterns the swarph encodes.

This package fills it as a pure Python library. Three repos make up
the v0.3.x architecture:

* ``swarph-mesh`` (this package)   — typed Protocol + adapters + SwarphCall.
                                     Pure library, no CLI.
* ``swarph-cli`` (separate repo)   — the ``swarph`` binary. Thin client
                                     on top of ``swarph-mesh``.
* ``swarph-meshlm`` (separate)     — Simon Willison ``llm`` plugin.

See the canonical PLAN at:
``https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md``

v0.1.0 — Phase 1 substrate. Ships:

* :class:`LLMAdapter` Protocol + ``ChatMessage`` + ``LLMResponse`` (from v0.0.1)
* :class:`SwarphCall` public surface — caller-validated, hook-wired entry-point
* :class:`GeminiAdapter` — wraps ``langgraph-genai-bridge`` (Flex tier, caching)
* JSON-mode harness — retry-once with [USER]-turn feedback (per PR #125 invariant)
* Attribution hooks + writers (FileAttributionWriter default;
  ``set_default_writer`` for production TSDB consumers)

Future phases (per PLAN.md §13):
  3      MeshClient — mesh-gateway HTTP wrapper
  4      DeepSeek + Claude (subscription) + OpenAI + Grok adapters
  2.5+   ``swarph import`` (lives in swarph-cli)
"""

from __future__ import annotations

# Public types
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

# Phase 1 surfaces
from swarph_mesh.swarph_call import SwarphCall
from swarph_mesh.adapters import get_adapter, register_adapter
from swarph_mesh.attribution import (
    AttributionEvent,
    AttributionWriter,
    FileAttributionWriter,
    NullAttributionWriter,
    get_default_writer,
    set_default_writer,
)
from swarph_mesh.hooks import (
    CallContext,
    HookSet,
    attribution_post_call,
    default_hooks,
)

# Phase 3 surfaces — MeshClient + types
from swarph_mesh.mesh_client import (
    MeshAuthError,
    MeshClient,
    MeshGatewayError,
    MeshSecretLeakError,
)
from swarph_mesh.mesh_types import MeshMessage, MeshPeer

__version__ = "0.2.0"

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
    # SwarphCall public surface
    "SwarphCall",
    "get_adapter",
    "register_adapter",
    # attribution
    "AttributionEvent",
    "AttributionWriter",
    "FileAttributionWriter",
    "NullAttributionWriter",
    "get_default_writer",
    "set_default_writer",
    # hooks
    "CallContext",
    "HookSet",
    "attribution_post_call",
    "default_hooks",
    # MeshClient (Phase 3)
    "MeshClient",
    "MeshPeer",
    "MeshMessage",
    "MeshGatewayError",
    "MeshAuthError",
    "MeshSecretLeakError",
]
