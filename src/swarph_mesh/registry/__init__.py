"""swarph-mesh adapter registry — the open adapter contract (P2).

Generalizes the hardcoded ``adapters/_REGISTRY`` so any popular agentic
CLI (node-wrap) or router (lane-register) can be declared + probed via a
uniform :class:`AdapterSpec` record.
"""

from __future__ import annotations

from swarph_mesh.registry.spec import AdapterSpec

__all__ = ["AdapterSpec"]
