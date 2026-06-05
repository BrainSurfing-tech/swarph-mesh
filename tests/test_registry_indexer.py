"""T6 — capability indexer publish hook + Skunkworks reference capability."""

from __future__ import annotations

from swarph_mesh.registry import (
    SKUNKWORKS_CAPABILITY,
    AdapterRegistry,
    capability_payload,
    make_indexer_hook,
    register_builtin_instances,
)
from swarph_mesh.registry.instances import OPENCLAW_SPEC, OPENROUTER_SPEC


def test_capability_payload_cli():
    payload = capability_payload(OPENCLAW_SPEC)
    assert payload["name"] == "openclaw"
    assert payload["kind"] == "cli"
    assert "DEEP_CODE" in payload["intents"]
    assert payload["models"] == []
    assert payload["invocation"]["channel"] == "MESH_TOOL"
    assert payload["invocation"]["input_channel"] == "argv"


def test_capability_payload_router():
    payload = capability_payload(OPENROUTER_SPEC)
    assert payload["kind"] == "router"
    assert payload["models"]  # non-empty
    assert payload["invocation"]["endpoint"] == "openai-compat"
    assert payload["invocation"]["channel"] == "MESH_TOOL"


def test_make_indexer_hook_publishes_on_register():
    hook = make_indexer_hook()
    assert callable(hook)
    reg = AdapterRegistry(on_register=hook)
    register_builtin_instances(reg, openclaw_runner=lambda p: "PONG")
    assert len(hook.published) == 2
    names = {p["name"] for p in hook.published}
    assert names == {"openclaw", "openrouter"}


def test_make_indexer_hook_custom_transport():
    captured = []
    hook = make_indexer_hook(transport=captured.append)
    reg = AdapterRegistry(on_register=hook)
    register_builtin_instances(reg, openclaw_runner=lambda p: "PONG")
    assert len(captured) == 2


def test_skunkworks_capability_constant():
    assert SKUNKWORKS_CAPABILITY["invocation"]["protocol"].startswith(
        "LAUNCH_SKUNKWORKS"
    )
    assert SKUNKWORKS_CAPABILITY["kind"] == "mesh_tool"
