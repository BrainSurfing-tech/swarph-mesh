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

**v0.0.1 — SCAFFOLD.** Typed substrate only:

- `LLMAdapter` Protocol (runtime-checkable)
- `ChatMessage`, `LLMResponse` pydantic dataclasses
- Exception hierarchy (`SwarphMeshError` → `AdapterError` / `UnknownProvider`)

No live adapters yet. Phase 1 ships Gemini + SwarphCall.

## Spec

The canonical PLAN with sequencing, falsifiability gates, and design rationale lives at:

→ [hedge-fund-mcp / research/swarph_cli/PLAN.md](https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md)

## Phase rollout

| Phase | Scope |
|---|---|
| **0** (this release) | Typed substrate — Protocol + dataclasses + exceptions |
| **1** | Gemini adapter + `SwarphCall` surface + caller convention import + Pydantic harness + 1 hook |
| **3** | `MeshClient` — replaces hand-rolled curl in `lab_loop_drain.py` etc. |
| **4** | DeepSeek + Claude (subscription) + OpenAI adapters |
| **5.5** | `swarph onboard` + `swarph ratify` (lives in `swarph-cli`, depends on this) |
| **5.7** | `swarph daemon` + REPL drain coroutine (lives in `swarph-cli`) |
| **6** | PyPI publish |
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
