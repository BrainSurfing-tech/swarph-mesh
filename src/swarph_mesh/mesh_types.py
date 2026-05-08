"""Typed dataclasses for mesh-gateway responses.

Shape derived from the live gateway at http://lab-ovh:8788 (2026-05-08).
Stable enough to type against; Phase 3+ migrations on the gateway side
will add fields rather than rename existing ones (per the swarph
conventions captured in CLAUDE.md hedge-fund-mcp).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class MeshPeer(BaseModel):
    """One row from ``GET /peers``."""

    name: str = Field(..., description="Canonical peer name per swarph_shared registry.")
    url: Optional[str] = Field(None, description="Tailnet URL of the peer's claude-service.")
    capabilities: dict[str, Any] = Field(
        default_factory=dict,
        description="Advert payload from peer registration: can_claim_tasks, runtime, "
        "session_ephemeral, etc.",
    )
    enabled: bool = Field(True, description="Gateway-side gating flag.")
    last_health: Optional[str] = Field(None, description="ISO timestamp of last health probe.")
    last_seen: Optional[str] = Field(None, description="ISO timestamp of last DM activity.")
    registered_at: Optional[str] = Field(None)

    # Forward-compat: gateway may add fields. Accept silently rather
    # than rejecting — adding mandatory fields here would break old
    # clients against new gateways.
    model_config = {"extra": "allow"}


class MeshMessage(BaseModel):
    """One row from ``GET /messages`` (or the success response of ``POST /messages``).

    v0.5.1 fix (drop DM #722 + #728): ``content`` is now ``Optional[str] = None``.
    The gateway's ``POST /messages`` success response returns
    ``{id, from_node, to_node, kind, thread_id, created_at}`` — no content field.
    Pre-fix, ``MeshClient.send`` raised ``pydantic.ValidationError`` on
    every successful POST despite the message landing cleanly, trapping
    callers wrapping ``send()`` in try/except into thinking the send failed
    and retrying — a DM duplication risk. The content-required model was
    correct for ``GET /messages`` rows (where content always present) but
    wrong for POST responses; making it optional fits both shapes.
    """

    id: int
    from_node: str
    to_node: str
    kind: str = Field(..., description='"fyi" | "question" | "answer" | "status" | "unblock"')
    content: Optional[str] = Field(
        None,
        description="Message body. Required on GET /messages rows; absent in "
        "POST /messages success responses.",
    )
    created_at: str = Field(..., description="ISO timestamp of mesh-gateway insert.")
    read_at: Optional[str] = Field(None, description="ISO timestamp when first marked read; None = unread.")
    related_task_id: Optional[str] = Field(None)
    thread_id: Optional[str] = Field(None)

    model_config = {"extra": "allow"}
