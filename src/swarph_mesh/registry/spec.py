"""AdapterSpec — the open adapter contract (P2 T1).

Generalizes the hardcoded ``adapters/_REGISTRY`` into a declared+probed
capability record for a wrapped tool. The §0 node-vs-lane distinction is
the master switch: ``kind="cli"`` wraps a sandboxed subprocess NODE,
``kind="router"`` registers an HTTP LANE. This model holds *declarations*;
probing (verifying the declared gates against the real binary) lands in a
later task.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, model_validator


class AdapterSpec(BaseModel):
    """Declared capability record for a wrapped tool.

    The kind-conditional shape is enforced post-construction by
    :meth:`_validate_kind_shape` — a ``cli`` is a sandboxed subprocess
    (needs an input channel + byte cap, exposes no model catalog); a
    ``router`` is an HTTP endpoint (exposes a model catalog, has no
    subprocess/sandbox affordances).
    """

    # identity
    name: str
    kind: Literal["cli", "router"]
    version: str
    license: str

    # the gates (declared here; PROBED elsewhere in a later task)
    headless_oneshot: bool
    input_channel: Optional[Literal["argv", "stdin", "file"]] = None  # cli only
    max_prompt_bytes: Optional[int] = None  # cli only
    sandbox_class: Optional[str] = None  # cli only, e.g. "firejail-fs-deny+net-allowed"
    billing_class: Literal["subscription_zero", "metered", "local_zero"]
    config_isolation: bool = True

    # declared capabilities
    structured_output: Literal["native", "prompt_discipline", "none"] = "prompt_discipline"
    session_continuity: bool = False
    streaming: bool = False

    # routing
    intents: list[Literal["MESH_TOOL", "CHIT_CHAT", "DEEP_CODE", "MESH_CC"]]
    models: list[str] = []  # router only: the model catalog it exposes

    # cli-only isolation binding
    enforced_isolation_class: Optional[str] = None
    provenance: Optional[str] = None

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> "AdapterSpec":
        if self.kind == "cli":
            if self.input_channel is None:
                raise ValueError("cli adapter requires input_channel")
            if self.max_prompt_bytes is None:
                raise ValueError("cli adapter requires max_prompt_bytes")
            if self.models:
                raise ValueError("cli adapter must not declare models (that is a router affordance)")
        elif self.kind == "router":
            if not self.models:
                raise ValueError("router adapter requires a non-empty models catalog")
            for field in (
                "input_channel",
                "max_prompt_bytes",
                "sandbox_class",
                "enforced_isolation_class",
            ):
                if getattr(self, field) is not None:
                    raise ValueError(
                        f"router adapter must not set {field} (an HTTP endpoint is not a sandboxed subprocess)"
                    )
        return self
