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

**v0.5.0 — Phase 4 complete.** All five adapters shipped: Gemini + DeepSeek + Claude (subscription) + OpenAI + Grok, plus Phase 3 MeshClient. Six PLAN.md §13 falsifiability gates PASSED end-to-end against live infrastructure (real Gemini API + real DeepSeek API + real lab-OVH mesh-gateway + real `claude -p` subscription path + real OpenAI API + real xAI API).

Public surface:

- `LLMAdapter` Protocol (runtime-checkable) + `ChatMessage` + `LLMResponse`
- `SwarphCall` — caller-convention-validated entry point with hooks + attribution
- `GeminiAdapter` — wraps `langgraph-genai-bridge` (Flex tier, context caching)
- `DeepSeekAdapter` (v0.3.0) — OpenAI-protocol-compatible client for V4-Flash / V4-Pro / V3 aliases; preserves reasoning content as `[reasoning]` preamble for portability
- `ClaudeAdapter` (v0.4.0) — subprocess-based wrapper around `claude -p` for **subscription billing path** (no `ANTHROPIC_API_KEY` needed; reads `~/.claude/.credentials.json`). Reuses `swarph_shared.scrub_env_for_subprocess` to keep billing-relevant env keys out of the subprocess. `cost_usd=0.0` for subscription calls (honest flat-rate); metered-equivalent cost preserved in `raw_response["api_metered_cost_usd"]` for auditors.
- **`OpenAIAdapter`** (NEW v0.5.0) — native async via `openai.AsyncOpenAI` (no `asyncio.to_thread` threadpool ceiling). Pricing for `gpt-4o` / `gpt-4o-mini` / `o1` / `o3` / `o3-mini` / `o4-mini` / `gpt-5`. o-series `reasoning_content` preserved as `[reasoning]` preamble. `OPENAI_API_KEY` env fallback.
- **`GrokAdapter`** (NEW v0.5.0) — xAI's OpenAI-compatible API at `https://api.x.ai/v1`, also via `AsyncOpenAI`. Pricing for `grok-4` / `grok-3` / `grok-3-mini` / `grok-2` (alias-routed). Dual env-var resolution: `XAI_API_KEY` (canonical) → `GROK_API_KEY` (alias). Same `[reasoning]` preamble shape as the rest of the adapter family.
- JSON-mode harness — retry-once with [USER]-turn feedback (per swarph-shared invariant)
- Attribution: `FileAttributionWriter` default; `set_default_writer()` for production TSDB consumers
- `MeshClient` (v0.2.0) — async wrapper around mesh-gateway HTTP API; replaces hand-rolled curl in `lab_loop_drain.py` / `mesh_inbox_watcher.py` / `science_claude_inbox_drain.py`

Tests: **174+ passing** (169 offline + 1 live claude subscription + 1 live deepseek + 2 live mesh + 1 live gemini + 1 live openai + 1 live grok smoke; live tests gated on respective env/creds).

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
| **3** (v0.2.0) | `MeshClient` async wrapper + recipient validation + mesh-secrets guard |
| **4 #2** (v0.3.0) | **DeepSeek adapter** — OpenAI-protocol-compatible, V4-Flash default, V4-Pro premium, V3 aliases preserved, reasoning_content kept as `[reasoning]` preamble |
| **4 #3** (v0.4.0) | **Claude subscription adapter** — wraps `claude -p` via subprocess, reads `~/.claude/.credentials.json`, billing-leak prevention via `scrub_env_for_subprocess` |
| **4 #4 + #5** (v0.5.0 — this release) | **OpenAI + Grok adapters** — both native-async via `AsyncOpenAI` from day one (per issue #7); xAI on `base_url=https://api.x.ai/v1`; Phase 4 ship-order complete |
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
