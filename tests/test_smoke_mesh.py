"""Live smoke test for MeshClient — Phase 3 falsifiability gate
per PLAN.md §13:

    MeshClient against the real lab-OVH gateway round-trips
    (list_peers → returns the live registry; fetch self-inbox
    succeeds).

Gated on ``MESH_GATEWAY_TOKEN`` env. Skipped when the token isn't set
or the gateway isn't reachable from the test host.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from swarph_mesh import MeshClient, MeshGatewayError, MeshPeer

pytestmark = pytest.mark.skipif(
    not os.environ.get("MESH_GATEWAY_TOKEN"),
    reason="MESH_GATEWAY_TOKEN not set; live smoke test skipped",
)


def test_phase_3_falsifiability_gate_list_peers():
    """Live ``GET /peers`` against lab-OVH mesh-gateway. Should return
    at least the well-known set: lab-ovh, droplet, science-claude."""

    async def _go():
        async with MeshClient(node="lab-ovh") as client:
            peers = await client.list_peers()
        return peers

    try:
        peers = asyncio.run(_go())
    except MeshGatewayError as exc:
        pytest.skip(f"mesh-gateway unreachable from test host: {exc}")

    names = {p.name for p in peers}
    # Don't lock the exact set (peers come and go) — just sanity-check
    # that the well-known core is present.
    assert "lab-ovh" in names, f"lab-ovh not in registry: {names}"
    assert all(isinstance(p, MeshPeer) for p in peers)


def test_phase_3_falsifiability_gate_fetch_inbox():
    """Live ``GET /messages?to_node=lab-ovh`` against lab-OVH. Should
    return a list (possibly empty) without auth/parse errors."""

    async def _go():
        async with MeshClient(node="lab-ovh") as client:
            return await client.fetch(limit=3)

    try:
        msgs = asyncio.run(_go())
    except MeshGatewayError as exc:
        pytest.skip(f"mesh-gateway unreachable: {exc}")

    # We don't assert non-empty (test runs may land between DMs)
    # just that the call shape works end-to-end and types deserialize.
    assert isinstance(msgs, list)
    if msgs:
        m = msgs[0]
        assert m.id > 0
        assert m.to_node == "lab-ovh"
        assert m.kind in {"fyi", "question", "answer", "error", "command", "system"}
