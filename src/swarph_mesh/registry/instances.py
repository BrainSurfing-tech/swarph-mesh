"""Worked AdapterSpec instances — the contract spans both kinds (P2 T5a).

Two concrete rows prove the open adapter contract holds for both a sandboxed
subprocess NODE (``kind="cli"``, OpenClaw) and an HTTP LANE (``kind="router"``,
OpenRouter). Values are the validated ones from the proven OpenClaw P1 scaffold
and the spec's OpenRouter design.
"""

from __future__ import annotations

from swarph_mesh.registry.registry import AdapterRegistry
from swarph_mesh.registry.spec import AdapterSpec

OPENCLAW_SPEC = AdapterSpec(
    name="openclaw",
    kind="cli",
    version="2026.6.2",
    license="MIT",
    headless_oneshot=True,
    input_channel="argv",
    max_prompt_bytes=120000,
    sandbox_class="firejail-fs-deny+net-allowed",
    billing_class="subscription_zero",
    config_isolation=True,
    structured_output="prompt_discipline",
    session_continuity=False,
    streaming=False,
    intents=["DEEP_CODE", "CHIT_CHAT", "MESH_TOOL"],
    enforced_isolation_class="firejail-fs-deny+net-allowed",
    provenance="openclaw.mjs sha256=ea04d15e53edc9ea git=a61c94b1",
)

OPENROUTER_SPEC = AdapterSpec(
    name="openrouter",
    kind="router",
    version="api-v1",
    license="proprietary",
    headless_oneshot=True,
    billing_class="metered",
    models=[
        "anthropic/claude-opus-4",
        "openai/gpt-5.5",
        "google/gemini-3-pro",
        "meta/llama-4",
    ],
    structured_output="native",
    session_continuity=False,
    streaming=True,
    intents=["DEEP_CODE", "CHIT_CHAT", "MESH_TOOL"],
)


def register_builtin_instances(registry: AdapterRegistry, *, openclaw_runner=None) -> None:
    """Register the built-in worked instances. openclaw_runner (optional) is the
    one-shot callable for OpenClaw's G1 probe; if None, G1 is BLOCKED (allowed)."""
    registry.register(OPENCLAW_SPEC, runner=openclaw_runner)
    registry.register(OPENROUTER_SPEC)  # router: no runner needed
