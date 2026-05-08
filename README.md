# swarph-mesh

Model-agnostic Python substrate for the swarph-mesh ecosystem. Pure library, no CLI.

Designed to fill the gap left by existing tools (`aichat`, `mods`, Simon Willison's `llm`, `gemini-cli`, `claude-cli`): none expose mesh-gateway participation, per-caller attribution, structured-output discipline, or the cooperative-protocol patterns the swarph encodes.

This is one of three repos in the v0.3.x architecture:

| Repo | Role |
|---|---|
| [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) | This package — typed Protocol + adapters + SwarphCall + MeshClient |
| [`swarph-cli`](https://github.com/darw007d/swarph-cli) | The `swarph` binary. Thin client over `swarph-mesh` |
| [`swarph-meshlm`](https://github.com/darw007d/swarph-meshlm) | Simon Willison `llm` plugin. Same primitives wired into `llm`'s plugin host |

All three sit on top of [`swarph-shared`](https://github.com/darw007d/swarph-shared) which provides the cross-billing-path attribution + subprocess env scrubbing + JSON-mode harness + peer-name registry primitives.

## Status

**v0.2.0 — Phase 1 substrate + Phase 3 MeshClient.** Both falsifiability gates from PLAN.md §13 PASSED end-to-end against live infrastructure (real Gemini API + real lab-OVH mesh-gateway).

Public surface:

- `LLMAdapter` Protocol (runtime-checkable) + `ChatMessage` + `LLMResponse`
- `SwarphCall` — caller-convention-validated entry point with hooks + attribution
- `GeminiAdapter` — wraps `langgraph-genai-bridge` (Flex tier, context caching)
- JSON-mode harness — retry-once with [USER]-turn feedback (per swarph-shared invariant)
- Attribution: `FileAttributionWriter` default; `set_default_writer()` for production TSDB consumers
- **`MeshClient`** (NEW v0.2.0) — async wrapper around mesh-gateway HTTP API; replaces hand-rolled curl in `lab_loop_drain.py` / `mesh_inbox_watcher.py` / `science_claude_inbox_drain.py`

Tests: **75/76 passing** (73 offline + 2 live mesh smoke + 1 live gemini smoke gated on env tokens).

```python
from swarph_mesh import SwarphCall, ChatMessage

# Phase 1 — LLM call with attribution
result = await SwarphCall(
    provider="gemini",
    caller="orchestrator.boss",
).chat(
    messages=[ChatMessage(role="user", content="hi")],
)
print(result.text, result.cost_usd, result.input_tokens)
```

```python
from swarph_mesh import MeshClient
import os

# Phase 3 — mesh-gateway DM coordination
async with MeshClient(node="lab-ovh") as client:  # token from MESH_GATEWAY_TOKEN env
    peers = await client.list_peers()
    msgs = await client.fetch(unread_only=True)
    sent = await client.send(to="droplet", kind="fyi", content="hello")
    await client.mark_read(msgs[0].id)
```

`MeshClient.send()` enforces two structural invariants:

1. **Recipient name validation** via `swarph_shared.validate_node_name` — closes the framing-contagion class (Vector A peer-onboarding chatter, Vector B human-prompt shorthand).
2. **Mesh-secrets out-of-band guard** — best-effort regex sniff for credential-shaped content (PyPI tokens, Anthropic keys, GitHub tokens, JWTs, AWS keys). Hits raise `MeshSecretLeakError` BEFORE the POST. Operator escape hatch via `skip_secret_check=True` for legitimate prose mentioning credential shapes. CLAUDE.md "Mesh secrets out-of-band only" is non-negotiable; the guard catches obvious cases.

## Spec

The canonical PLAN with sequencing, falsifiability gates, and design rationale lives at:

→ [hedge-fund-mcp / research/swarph_cli/PLAN.md](https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md)

## Phase rollout

| Phase | Scope |
|---|---|
| **0** (v0.0.1) | Typed substrate — Protocol + dataclasses + exceptions |
| **1** (v0.1.0) | Gemini adapter + `SwarphCall` surface + caller convention import + JSON-mode harness + attribution hook |
| **3** (v0.2.0 — this release) | `MeshClient` async wrapper + recipient validation + mesh-secrets guard |
| **4** | DeepSeek + Claude (subscription) + OpenAI adapters |
| **5.5** | `swarph onboard` + `swarph ratify` (lives in `swarph-cli`, depends on this) |
| **5.7** | `swarph daemon` + REPL drain coroutine (lives in `swarph-cli`) |
| **6** | (already done) PyPI publish |
| **7** | `swarph-meshlm` plugin (separate repo, this dep) |

## Install (dev)

```bash
git clone https://github.com/darw007d/swarph-mesh
cd swarph-mesh
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT. Pierre Samson + Claude Opus, 2026.
