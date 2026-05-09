# swarph-mesh

[![PyPI](https://img.shields.io/pypi/v/swarph-mesh.svg)](https://pypi.org/project/swarph-mesh/)
[![Python](https://img.shields.io/pypi/pyversions/swarph-mesh.svg)](https://pypi.org/project/swarph-mesh/)
[![Docs](https://img.shields.io/badge/docs-darw007d.github.io-blue)](https://darw007d.github.io/swarph-mesh/)
[![codecov](https://codecov.io/gh/darw007d/swarph-mesh/graph/badge.svg)](https://codecov.io/gh/darw007d/swarph-mesh)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

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

**v0.6.1 — PRICING table catch-up + alias resolution.** Patches v0.6.0's discovery substrate with corrected pricing for ~20 models surfaced by the AIMLAPI catalog diff:

- **Critical fix:** `gpt-5` was at `(5.00, 20.00)` speculative in v0.5.x; real direct OpenAI pricing is `(1.25, 10.00)` — anyone calling `gpt-5` between v0.5.x and v0.6.1 had `cost_usd` over-attributed by ~4x.
- New OpenAI entries: `gpt-4.1` family (4.1 / 4.1-mini / 4.1-nano), `gpt-5-mini`, `gpt-5-nano`, `gpt-5.2`, `gpt-5.2-pro`.
- New Anthropic entries: `claude-opus-4-1` (premium $15/$75 tier), `claude-sonnet-4-5`, `claude-sonnet-4`, `claude-haiku-3-5`, `claude-haiku-3` + dated-build aliases (`claude-opus-4-5-20251101`, etc.).
- New xAI entries: `grok-4-3`, `grok-4-20-*`, `grok-4-1-fast-*`, `grok-4-fast-*` (current generation per xAI docs). **`grok-4` + `grok-code-fast-1` retire 2026-05-15** per xAI; v0.6.1 documents this in `_GROK_RETIREMENT_NOTICE`.
- New xAI / DeepSeek alias normalizers: `_normalize_xai_id` strips `x-ai/` prefix + `-beta` + dated suffixes; `_normalize_deepseek_id` strips `deepseek/` prefix + version suffixes. AIMLAPI's prefixed catalog IDs now resolve to PRICING entries instead of falling through to `_default`.
- New Gemini entries: `gemini-2.0-flash`, `gemini-2.0-flash-001`.
- `_OPENAI_PRICING_VERIFIED_AT` / `_GROK_PRICING_VERIFIED_AT` / `_GEMINI_PRICING_VERIFIED_AT` metadata added for future drift detection (drop DM #720 direction).

**v0.6.0 — architectural promotion: model discovery substrate.** Four primitives:
- Catalog (AIMLAPI primary + per-provider fallback)
- Gemini pricing (Cloud Billing Catalog API)
- Anthropic pricing (manual table from claude.com/pricing with `verified_at` provenance)
- OpenAI cost reconciliation (admin-key gated `/v1/organization/costs`)
- `LLMAdapter.list_models()` Protocol method (breaking change → v0.6.0 major-version bump)

Public surface:

- `LLMAdapter` Protocol (runtime-checkable) + `ChatMessage` + `LLMResponse`
- `SwarphCall` — caller-convention-validated entry point with hooks + attribution
- `GeminiAdapter` — wraps `langgraph-genai-bridge` (Flex tier, context caching)
- `DeepSeekAdapter` (v0.3.0) — OpenAI-protocol-compatible client for V4-Flash / V4-Pro / V3 aliases; preserves reasoning content as `[reasoning]` preamble for portability
- `ClaudeAdapter` (v0.4.0) — subprocess-based wrapper around `claude -p` for **subscription billing path** (no `ANTHROPIC_API_KEY` needed; reads `~/.claude/.credentials.json`). Reuses `swarph_shared.scrub_env_for_subprocess` to keep billing-relevant env keys out of the subprocess. `cost_usd=0.0` for subscription calls (honest flat-rate); metered-equivalent cost preserved in `raw_response["api_metered_cost_usd"]` for auditors.
- `OpenAIAdapter` (v0.5.0) — native async via `openai.AsyncOpenAI` (no `asyncio.to_thread` threadpool ceiling). Pricing for `gpt-4o` / `gpt-4o-mini` / `o1` / `o3` / `o3-mini` / `o4-mini` / `gpt-5`. o-series `reasoning_content` preserved as `[reasoning]` preamble. `OPENAI_API_KEY` env fallback.
- `GrokAdapter` (v0.5.0) — xAI's OpenAI-compatible API at `https://api.x.ai/v1`, also via `AsyncOpenAI`. Pricing for `grok-4` / `grok-3` / `grok-3-mini`. Dual env-var resolution: `XAI_API_KEY` (canonical) → `GROK_API_KEY` (alias). Same `[reasoning]` preamble shape as the rest of the adapter family.
- **`swarph_mesh.discovery`** (NEW v0.6.0) — three primitives:
  - **Catalog** backed by [AIMLAPI's public `/models` endpoint](https://docs.aimlapi.com/api-references/service-endpoints/complete-model-list) (no auth, ~600+ entries) with per-provider `/v1/models` fallback when AIMLAPI is unreachable. `list_models(provider=...)`, `is_model_supported(model_id)`, `get_model_info(model_id)`. 24h TTL cache.
  - **Gemini pricing** via Google's [Cloud Billing Catalog API](https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list) (service `241C-273D-49C8` = Vertex AI). `fetch_gemini_pricing()` and `pricing_for_gemini_model(hint, direction, tier)`. Auth: `GOOGLE_CLOUD_BILLING_API_KEY` env (separate from `GEMINI_API_KEY` — Cloud Console project key with Billing API scope).
  - **Anthropic pricing** via static manual table mirrored from claude.com/pricing (Anthropic does not expose programmatic pricing). `pricing_for_anthropic_model(id)` returns full 5-tuple (base input + 5m cache + 1h cache + cache hit + output) per model. Honest about origin via `verified_at` field; update by re-pasting the docs table.
  - OpenAI/xAI/DeepSeek pricing stays in adapter-local `PRICING` dicts until programmatic sources surface.
- **`LLMAdapter.list_models()`** (NEW v0.6.0) — Protocol-level method. Each adapter delegates to `discovery.list_models(provider=self.name)`. Breaking change: external implementations of `LLMAdapter` from v0.5.x must implement this method to satisfy the runtime-checkable Protocol.
- JSON-mode harness — retry-once with [USER]-turn feedback (per swarph-shared invariant)
- Attribution: `FileAttributionWriter` default; `set_default_writer()` for production TSDB consumers
- `MeshClient` (v0.2.0) — async wrapper around mesh-gateway HTTP API; replaces hand-rolled curl in `lab_loop_drain.py` / `mesh_inbox_watcher.py` / `science_claude_inbox_drain.py`

Tests: **253+ passing** (250 offline + live gates: 1 claude subscription + 1 deepseek + 2 mesh + 1 gemini + 1 openai + 1 grok + 3 discovery against live AIMLAPI; live tests gated on env/creds or `SWARPH_SKIP_NETWORK=1`).

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
