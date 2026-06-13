# Changelog

All notable changes to `swarph-mesh` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) per the policy in [DEPRECATIONS.md](./DEPRECATIONS.md).

`swarph-mesh` is the graph-protocol substrate for the swarph-mesh ecosystem — every CLI is a node, the `LLMAdapter` Protocol is the third-party node-implementer contract. Changes to that contract follow strict policy; ordinary feature additions follow ordinary semver.

## [Unreleased]

### Foundations sprint queue

> **Renumber note (2026-05-21):** the queued node-implementer guide slot shifted v0.7.5 → v0.7.6 (and transport-agnostic mesh → v0.7.7) because v0.7.5 shipped the GeminiCLIAdapter — built, tested, and ready ahead of the guide. Shipped-and-tested takes the lower number; the planned ordering below is otherwise unchanged.

- **v0.7.6** — Node-implementer guide (`docs/becoming-a-node.md`) + protocol-stability test ratchet (mypy strict expanded one module)
- **[Mesh-as-OS PLAN.md §X draft slot]** — service-mode LLM workers + ephemeral CLI clients per commander chimes 2026-05-09; auth-surface-minimization framing per a design review; targets slot between v0.7.6 + v0.7.7
- **v0.7.7+** — Transport-agnostic mesh (Tailscale optional; mTLS + WireGuard + cloud VPN paths) — pairs with commander's parallel `/btw` session on mesh-of-meshes federation; load-bearing prereq for mesh-as-OS service-worker cross-host dispatch

## [0.7.9] — 2026-06-13

### Added

- **`GrokCLIAdapter` — `provider="grok-cli"`** — firejail-sandboxed `grok --prompt-file` **subscription** path (xAI Grok Build CLI, model `grok-build`, 512K ctx), the **$0 sibling of the metered `grok` adapter** exactly as `AntigravityAdapter` is the subscription sibling of the metered Gemini adapters. Auth is an OIDC **session token** (`~/.grok/auth.json`) riding a SuperGrok-Heavy subscription → `cost_usd=0.0`, no metered `XAI_API_KEY`.
  - **Security — grok is agentic + may run `permission_mode=always-approve`.** Every call runs in a **firejail OS sandbox** identical in shape to the agy lane (`--caps.drop=all --nonewprivs --noroot --seccomp --private-tmp`, whitelist only the grok binary + `~/.grok` runtime dir + the prompt-file dir). firejail is the sole, load-bearing containment.
  - **Prompt-file hardening (drop seat-A review):** prompt written via `os.open(O_EXCL, 0o600)` in a `0700` dir **outside** `~/.grok` (was world-readable `0664` host-side, written before firejail); `--blacklist ~/.grok/memory` seals the cross-session memory contamination store at the fs layer (beyond the `--no-memory` flag); `_scrubbed_env` strips `XAI_API_KEY`/`GROK_API_KEY`/`GROK_CODE_XAI_API_KEY` **plus** the endpoint-redirect class `XAI_API_BASE`/`XAI_API_HOST`/`GROK_API_HOST`; `--no-memory --no-subagents`; `_resolve_grok_bin` rejects a relative `GROK_BIN` (anti-PATH-shadow).
  - **Stateless by construction** — `--no-memory` so a mesh call never reads/writes any operator's personal grok memory. Plain-text output → token counts `0/0`; per-call audit log (firejail `--trace` is seccomp-incompatible). 22 unit tests; full suite green; live smoke (firejail + real grok) verified.
- **`__version__` synced** to 0.7.9 in both `pyproject.toml` and `__init__.py` (drift guard).

> Note: the `[0.7.8]` release ("adversarial-sweep hardening", #24) shipped without a CHANGELOG section; recorded here for the gap. Its changes are in PRs #22/#23.

## [0.7.7] — 2026-05-23

### Added

- **`AntigravityAdapter` — `provider="antigravity"`** — firejail-sandboxed `agy -p` subscription path (gemini-3.5-flash), the **successor to `GeminiCLIAdapter`** for Google's **June-2026 gemini-CLI discontinuation**. agy reuses `~/.gemini/antigravity-cli/antigravity-oauth-token` (subscription, `cost_usd=0.0`) and unlocks 3.5-flash (preview-gated on the old gemini CLI).
  - **Security — agy is agentic.** Spike found `agy -p` auto-fires fs/bash tools, has no `--no-tools` flag, and `--sandbox` doesn't contain the filesystem. So every call runs inside a **firejail OS sandbox** whitelisting only agy's binary + its `~/.gemini/antigravity-cli` runtime dir, `--caps.drop=all --nonewprivs --noroot --seccomp --private-tmp`. Verified: a read outside the whitelist is BLOCKED; the LLM call still works. fs blast-radius sealed. Residual (accepted v1): network egress stays open (the LLM call needs 443) — documented.
  - **Billing-leak guard extended:** `scrub_env_for_subprocess` strips `*_API_KEY` by suffix but missed `GOOGLE_APPLICATION_CREDENTIALS` + project vars (`GOOGLE_CLOUD_PROJECT`, `VERTEX_*`); the adapter strips them explicitly so no metered Vertex/GCP fallback can fire.
  - **Per-call audit log** (rollout-observation): `{ts, prompt_sha8, exit, duration, resp_len, timed_out, stderr_tail}` to `swarph_audit.jsonl`. firejail `--trace` is seccomp-incompatible, so observation happens at the adapter layer (semantic signal > syscall noise). 14 unit tests.
  - **Dual-runs with `gemini-cli`** until the June EOL; AI²-reviewed end-to-end with a review peer.
- **Version drift fix:** `__init__.__version__` was stale at 0.7.4 (the 0.7.5/0.7.6 publishes bumped only `pyproject`); now synced to 0.7.7.

## [0.7.6] — 2026-05-21

### Changed

- **`GeminiCLIAdapter` default model pinned to `gemini-2.5-flash`** (was: empty string → let the CLI pick). The CLI's own default routes to `gemini-3-flash-preview`, and the whole gemini-3.x Flash line (incl. the just-released 3.5 Flash, 2026-05-19) is **Preview status** — throttle-prone + timeout-prone on the subscription tier (observed: intermittent 30s+ timeouts even back-to-back). Pinning the GA model gives workers a stable, reliable default. Override anytime with no redeploy via `GEMINI_SUBSCRIPTION_MODEL` (e.g. set it to a 3.5 Flash preview id once it graduates to GA / is enabled on your subscription).

## [0.7.5] — 2026-05-21

### Added

- **`GeminiCLIAdapter` — `provider="gemini-cli"`** — subscription-billed Gemini via the `gemini` CLI binary (`gemini -p -o json`), the Google mirror of `ClaudeAdapter`'s `claude -p` path. Reads `~/.gemini/oauth_creds.json` (the CLI's OAuth login) and bills flat-rate through the user's Google subscription — `cost_usd=0.0`, `raw_response["billing_path"]="subscription"`. Metered-equivalent cost preserved in `raw_response["api_metered_cost_usd"]` for subscription-savings auditing.
  - **Billing-leak prevention** — `GEMINI_API_KEY` / `GOOGLE_API_KEY` are scrubbed from the subprocess env via `swarph_shared.scrub_env_for_subprocess` (explicit denylist + `*_API_KEY` suffix), so a key sitting in the env can't silently flip the CLI to metered billing. Proven: invoking with `GEMINI_API_KEY=LEAK_CANARY` in the env still bills subscription.
  - **Token accounting** — `_aggregate_tokens` sums `prompt` (input) + `candidates` (output) + `cached` across every model in `stats.models` (the CLI may route a utility model plus the main model per call).
  - **Why a new provider name, not a replacement for `gemini`** — the metered SDK path (`provider="gemini"` → `GenAIBridge`, Flex rebate + Pro context caching) stays the default. Subscription billing is opt-in via `--provider gemini-cli` so no caller is silently re-routed onto a personal Google subscription.
  - 21 unit tests (`tests/test_gemini_cli_adapter.py`), offline-mocked, mirroring the Claude adapter test layout.

## [0.7.4] — 2026-05-09

### Added

- **Coverage publication via Codecov** — pytest-cov branch + line coverage measured in CI on the 3.13 matrix arm, uploaded via `codecov/codecov-action@v4`. Public repo (flipped 2026-05-09 by commander auth) means no token required.
- **`codecov.yml`** — coverage policy mirrors `DEPRECATIONS.md` tier discipline:
  - Project regression tolerance: 1% drop while we ratchet upward (auto-target)
  - **Patch coverage gate at 70%** — new code in a PR must be covered; old uncovered code is grandfathered, fresh uncovered code blocks merge
  - Components grouped by Tier (Tier 1 Protocol / Tier 1 discovery / Tier 2 SwarphCall+hooks+attribution / Tier 3 adapters) so the dashboard mirrors the contract taxonomy
  - Ignored: tests/, `__init__.py` (re-exports), `_build/`, `docs/`
- **Coverage badges** in README.md — PyPI version + Python pyversions + docs site link + Codecov + MIT license
- **`pytest-cov>=4.0`** added to `[project.optional-dependencies] dev`

### Notes

- **Baseline coverage at v0.7.4: 90.0% line / 87.6% branch** across 1316 statements, 330 branches. Per-module: 100% types/exceptions/hooks/mesh_types, 96% mesh_client, 93% gemini/swarph_call, 92% deepseek, 91% json_harness, 88% grok, 87% discovery, 86% openai, 83% claude, 98% attribution.
- The 5th hardening dimension after v0.7.3 (doc-surface) is now **coverage-surface lockstep** — Codecov patch gate forces fresh code to be tested at PR time, same enforcement-at-merge-time pattern as Sphinx strict-mode autodoc.
- `fail_ci_if_error: false` on the codecov upload step — Codecov outage shouldn't block merges; the local coverage XML is still authoritative if upload fails.

## [0.7.3] — 2026-05-09

### Added

- **Sphinx auto-docs build** — first browsable contract surface for third-party node implementers:
  - `docs/conf.py` — autodoc + autosummary + napoleon + intersphinx + viewcode + myst-parser; furo theme; pulls version from package `__init__` so docs / pyproject never drift.
  - Per-module `docs/api/*.rst` files for the 8 public modules (`types`, `adapters`, `swarph_call`, `mesh_client`, `discovery`, `attribution`, `hooks`, `exceptions`); each rst marks the Tier 1/2/3 stability of the module's surface up front.
  - `docs/index.md` lands the package, links the four-package ecosystem (swarph-shared / swarph-mesh / swarph-cli / swarph-meshlm), and points at the contract anchors (`SECURITY.md` / `DEPRECATIONS.md` / `CHANGELOG.md`).
  - `CHANGELOG.md`, `DEPRECATIONS.md`, `SECURITY.md` included via myst-parser `{include}` so the canonical files stay GitHub-rendered + the doc site stays single-source-of-truth.
- **GitHub Pages deploy** — `.github/workflows/docs.yml`:
  - On `main` push: build with `SPHINXOPTS=-W --keep-going` (warnings = errors) + deploy via `actions/deploy-pages@v4`.
  - On PR: build only (no deploy) so reviewers see green-or-red before merge.
  - Concurrency group prevents overlapping deploys.
- **`docs` extras in `pyproject.toml`** — `pip install swarph-mesh[docs]` bootstraps `sphinx>=7.0` + `furo` + `myst-parser`.

### Fixed

- **Python 3.10 support honored** — `discovery.py` had three `_dt.UTC` callsites (Python 3.11+ only) at lines 424, 702, 933, despite `pyproject.toml` declaring `requires-python = ">=3.10"`. Replaced with `_dt.timezone.utc` (works on all 3.10+). The same failure was silently present on PRs #17/#18/#19 (v0.7.0/v0.7.1/v0.7.2) where the pytest 3.10 matrix arm was treated as non-blocking; v0.7.3 review caught the discipline drift and fixed it in-PR. Discovered tests on Python 3.11/3.13 always passed; only 3.10 leg failed with `AttributeError: module 'datetime' has no attribute 'UTC'`. Honors the declared contract surface in keeping with the doc-surface-as-hardening framing of this release.
- **`docs/_static/.gitkeep`** added — empty directory wasn't tracked by git, causing strict-mode `sphinx-build -W` to fail in CI on `html_static_path entry '_static' does not exist` (worked locally because Sphinx tolerates missing `_static` in non-strict mode). Caught by drop on first CI invocation — confirms the doc-surface-as-contract-enforcement hypothesis on its first real run.

### Notes

- Strict-mode build catches autodoc misses (missing docstring on a public symbol, broken intersphinx link) so the contract documentation stays in lockstep with the contract.
- One docstring formatting fix in `src/swarph_mesh/swarph_call.py` (definition-list-followed-by-Usage-block triggered docutils warning under strict mode); rewrote as Google-style napoleon prose + `Example::` block.
- Fixes a 404 intersphinx inventory reference (`https://www.python-httpx.org/objects.inv` doesn't exist); httpx removed from `intersphinx_mapping`.

## [0.7.2] — 2026-05-09

### Added

- **`CHANGELOG.md`** (Keep-a-Changelog 1.1.0) — full release history v0.0.1 → v0.7.1 with Added/Changed/Fixed/Notes sections per release; CRITICAL fixes called out in version entries (gpt-5 5x + o3 5x over-attribution).
- **`DEPRECATIONS.md`** — versioning + deprecation policy:
  - 3-tier contract surface (Tier 1 highest = `LLMAdapter` Protocol + `ChatMessage` + `LLMResponse` + canonical adapter names + `MeshClient.send`/`fetch` + `MeshMessage` + discovery primitives; Tier 2 medium = `SwarphCall` + `AttributionWriter` + `HookSet` + `normalize_*_id` + `_RETIREMENT_REGISTRY` keys; Tier 3 lower = adapter `PRICING` dicts + adapter-local `_normalize` copies + `_ANTHROPIC_PRICING` contents)
  - Per-tier breaking-change rules (e.g. adding `LLMAdapter` method = BREAKING under runtime-checkable Protocol, adding `MeshMessage` field with default = NON-BREAKING, renaming canonical adapter name = MAJOR-only)
  - Deprecation cycle: announcement + `DeprecationWarning` + min 1 MINOR cycle + CHANGELOG entry + migration paragraph for Tier 1
  - Mypy `--strict` module ratchet schedule v0.7.0 → v0.8.0
  - Hardening commitment tracker (extends `SECURITY.md`)
  - Closing discipline: "When in doubt, default is YES it's breaking"

### Notes

- Resolves all 8 forward-references from `SECURITY.md` v0.7.1, `tests/test_protocol_contract.py` failure messages, and CI workflow ratchet comment.
- Pure governance artifact; no code change. Tests: 293 passed + 5 skipped — unchanged.

### Feature track queue (after foundations stable)

- LLM-scraper primitive (subscription-default opt-in per commander direction; cross-check optional)
- Drift-detection cron (uses `verified_at` metadata across PRICING tables)
- Cached-input pricing tuple-shape extension (`(input, cached, output)` triples)
- Retirement-warning runtime hook at adapter dispatch (uses `discovery.is_retired`)
- gpt-5.3-codex / gpt-5.2-codex pricing (deferred from v0.6.2; needs commander pasting from authoritative OpenAI source)

## [0.7.1] — 2026-05-09

### Added

- **`SECURITY.md`** — security policy with 9 explicit in-scope vulnerability classes mapped to actual contract surface (NOT generic boilerplate):
  1. Credential leakage (logs, attribution, exception messages, DM contents, PyPI artifacts)
  2. Privilege boundary bypass (regular → admin keys, subscription → metered exfiltration)
  3. Cost over-attribution (the gpt-5 5x and o3 5x classes elevated to first-class security concern at enterprise spend)
  4. Cost under-attribution (audit + quota escape)
  5. Mesh-gateway authentication bypass (`/peers`, `/messages`, `/tasks/claim`)
  6. Ratification bypass (`task_claim` for `ratified=false`, `peer_ratifications` mutability)
  7. Protocol contract violation (`LLMAdapter` / `MeshMessage` / `MeshClient.send` / `discovery` API changes without deprecation window — anchored in [DEPRECATIONS.md](./DEPRECATIONS.md))
  8. Catalog poisoning (crafted `ModelInfo` records via AIMLAPI or `/v1/models`)
  9. DM contagion (a peer-name-alias drift class (e.g. `db` vs `database-node`) promoted to security concern; pre-federation cross-mesh status leaks)
- Reporting channels: `pierresamson@gmail.com` with `[swarph-mesh security]` subject prefix, GitHub Security Advisory (preferred for repo-scoped findings).
- Response timeline commitments: 48h ack / 5 business days triage / 14d high-critical fix / 30d medium / 90d low.
- Hardening commitment per minor release (at least one of: secret-guard regex tightening, protocol-stability snapshot expansion, mypy strict module ratchet, privilege-boundary test addition).
- Federation forward-compat note for v0.8+ mesh-to-mesh work.

### Notes

Pure governance artifact addition. No Protocol changes. No API removals. Backward-compat with v0.7.0.

## [0.7.0] — 2026-05-09

### Added

- **GitHub Actions CI workflow** (`.github/workflows/ci.yml`):
  - Pytest matrix: Python 3.10 (declared floor) + 3.13 (declared ceiling)
  - `mypy --strict` gate scoped to `types.py` + `exceptions.py` (ratchets per minor per [DEPRECATIONS.md](./DEPRECATIONS.md))
  - Ruff lint (F-rule subset, non-blocking initially)
  - Concurrency cancellation on force-push
  - `SWARPH_SKIP_NETWORK=1` in CI runners (live AIMLAPI catalog smoke + cost-recon don't hit network from PR runners)

- **PEP 561 type marker** (`src/swarph_mesh/py.typed`):
  - Empty file packaged via `tool.setuptools.package-data`
  - Third-party type-checkers (mypy, pyright, etc.) now see swarph-mesh's hints
  - Was missing pre-v0.7.0 — every external pip-install consumer got `Untyped` annotations from us until they explicitly opted in

- **Protocol-stability snapshot tests** (`tests/test_protocol_contract.py`, 10 tests):
  - `LLMAdapter` `@runtime_checkable` preservation (third-party `isinstance()` workflow guarantee)
  - Required class attribute set (`name`, `default_model`)
  - Frozen method set: `chat` / `stream` / `cost_per_token` / `list_models`
  - `chat()` signature kwarg-default frozen (`system_prompt=None`, `json_schema=None`, `temperature=0.7`, `max_tokens=None`)
  - `list_models()` `ttl_seconds=86400` default frozen
  - `ChatMessage` + `LLMResponse` field-set guarantees
  - Canonical adapter names `{gemini, deepseek, claude, openai, grok}` reserved + dispatchable
  - Discovery public API frozen at v0.7.0 baseline

### Changed

- `LLMResponse.parsed` typed as `dict[str, Any]` (was bare `dict`) for `mypy --strict`.
- `LLMResponse.raw_response` typed as `dict[str, Any]`.
- `LLMAdapter.chat` `json_schema` typed as `dict[str, Any]`.
- `LLMAdapter.list_models` return typed as `list[Any]`.

### Fixed

- Test fixture `test_canonical_adapter_names_in_registry_dispatch` was caching fake-key adapters in the singleton registry, silently poisoning live smoke tests later in the same run. Wrapped in `reset_registry()` try/finally. Caught when full pytest run started failing `tests/test_smoke_deepseek` with 401 errors from leftover fake keys. The class is now documented internally.

### Notes

Major-style minor signals foundations-layer shift in v0.7.x line. Each subsequent v0.7.x minor adds one foundations item. Backward-compat with v0.6.x: no Protocol changes, no API removals.

## [0.6.2] — 2026-05-09

### Added

- **OpenAI PRICING entries** from authoritative `openai.com/api/pricing` source (commander-pasted 2026-05-09):
  - gpt-5.1 family: `gpt-5.1`, `gpt-5.1-codex`, `gpt-5.1-codex-mini`
  - gpt-5.4 family: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.4-pro`
  - gpt-5.5 family: `gpt-5.5`, `gpt-5.5-pro`
  - `gpt-5-pro` (premium tier $15/$120)
  - `o1-pro` (premium reasoner $150/$600)
  - `o3-pro` ($20/$80)

- **Centralized normalizers** in `swarph_mesh.discovery`:
  - `normalize_xai_id`, `normalize_deepseek_id`, `normalize_model_id` (provider-aware dispatcher)
  - Adapter-local `_normalize_*_id` copies preserved for back-compat

- **Shared retirement registry**:
  - `_RETIREMENT_REGISTRY` dict keyed by `provider/model_id` → ISO date OR `"deprecated"` sentinel
  - Public API: `is_retired(provider, model_id, today=None)` + `retirement_date(provider, model_id)`
  - Initial entries: `grok/grok-4` + `grok/grok-code-fast-1` (2026-05-15); `claude/claude-sonnet-3-7` + `claude/claude-opus-3` (deprecated sentinels)

- Provenance metadata: `_OPENAI_PRICING_VERIFIED_AT = "2026-05-09"` + `_OPENAI_PRICING_SOURCE`.

### Fixed

- **`o3` over-attribution** (CRITICAL): was `(10.00, 40.00)` speculative in v0.6.1; real direct OpenAI pricing is `(2.00, 8.00)`. **5x over-attribution** for any caller using `o3` between v0.5.x and v0.6.1. Same class as the `gpt-5` fix in v0.6.1. Regression test asserts the corrected numbers.
- `o1-mini` correction: `(3.00, 12.00)` → `(1.10, 4.40)` (matches `o3-mini`/`o4-mini` tier).

### Deferred

- `gpt-5.3-codex` / `gpt-5.2-codex`: not on the standard-tier source page commander pasted; route to `_default` until next verification cycle.
- Cached-input pricing tuple-shape extension (page exposes `(input, cached, output)` triples; tracked as feature-queue item).
- Retirement-warning runtime hook at adapter dispatch.

## [0.6.1] — 2026-05-08

### Fixed

- **`gpt-5` over-attribution** (CRITICAL): was `(5.00, 20.00)` speculative in v0.5.x; real direct OpenAI pricing per Helicone is `(1.25, 10.00)`. ~4x over-attribution window for callers using `gpt-5` pre-v0.6.1.

### Added

- 9 carry-forward fixes batched: gpt-4.1 family, gpt-5-mini/nano, claude-opus-4-1 + dated builds, grok-4-fast family, gemini-2.0-flash, alias normalizers, secret-guard regex tightenings, MeshClient response-shape fixes, kind enum, ?to= wire fix.
- `fetch_xai_cost_buckets()` + `reconcile_xai_cost()` via `management-api.x.ai/v1/billing/teams/{id}/usage` (live smoke verified).
- `fetch_deepseek_balance()` via `/user/balance` (live smoke verified).

### Notes

PR #14 was auto-closed when its stacked base (PR #13 `feat/v0-6-0-discovery`) was deleted on merge; recovered by opening PR #15 with `base=main` carrying the prior approval forward. Class documented internally.

## [0.6.0] — 2026-05-09

**Architectural promotion: model discovery substrate. BREAKING CHANGE on `LLMAdapter` Protocol.**

### Added

- **`swarph_mesh.discovery` module** with 4 primitives:
  - **Catalog** backed by [AIMLAPI's public `/models` endpoint](https://docs.aimlapi.com/api-references/service-endpoints/complete-model-list) (~600 entries, no auth, 24h TTL) + per-provider `/v1/models` OpenAI-compat fallback
  - **Gemini pricing** via Google's [Cloud Billing Catalog API](https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list) (service `241C-273D-49C8` = Vertex AI)
  - **Anthropic pricing** via static manual table from `claude.com/pricing` with `verified_at` provenance
  - **OpenAI cost reconciliation** via `/v1/organization/costs` admin-key gated; live smoke PASSED end-to-end
- `LLMAdapter.list_models()` Protocol method — **BREAKING CHANGE**: external implementations of `LLMAdapter` from v0.5.x must add this method to satisfy the runtime-checkable Protocol. Each shipped adapter delegates via 3-line lazy-import call.
- Secret-guard regex tightening: `sk-admin-` named pattern with elevated-privilege-class error message.

### Notes

v0.6.0 not published to PyPI (v0.6.1 supersedes); git tag `v0.6.0` exists for provenance.

## [0.5.1] — 2026-05-08

### Fixed

- `MeshClient.send` response shape mismatch (pydantic ValidationError on success POST despite delivery — DM duplication risk).
- `MeshClient.fetch` `?to=` wire-shape fix (was using `?to_node=` which the gateway silently ignored).

### Added

- 9 carry-forward fixes: secret-guard regex tightenings (sk-proj-, xai-, deepseek explicit, AIza, mesh-gateway tokens), DEEPSEEK_LEGACY_ENV_PATH override, V4-Pro promo verify-after sentinel, claude.py once-per-instance silent-kwargs warnings, CLAUDE_BIN docstring, grok-2 PRICING removal, OpenAI `_default` → gpt-4o-mini under-bill-on-uncertainty.
- `kind=Literal[...]` enum on `MeshClient.send` for fail-fast.

## [0.5.0] — 2026-05-08

### Added

- **Phase 4 #4 + #5**: `OpenAIAdapter` + `GrokAdapter` (native AsyncOpenAI from day one per drop's #7 forward-direction). xAI uses OpenAI-protocol-compatible API at `https://api.x.ai/v1`. Reasoning preservation `[reasoning]` shape consistent across all four reasoner-capable adapters.

## [0.4.0] — 2026-05-08

### Added

- **Phase 4 #3**: `ClaudeAdapter` subscription path via `claude -p` subprocess. Reads `~/.claude/.credentials.json`. Billing-leak prevention via `swarph_shared.scrub_env_for_subprocess` strips ANTHROPIC_API_KEY before invoking. `cost_usd=0.0` for subscription (honest flat-rate); metered-equivalent in `raw_response["api_metered_cost_usd"]` for auditors.

## [0.3.0] — 2026-05-08

### Added

- **Phase 4 #2**: `DeepSeekAdapter` (OpenAI-protocol-compatible client for V4-Flash / V4-Pro / V3 aliases). Preserves reasoning content as `[reasoning]` preamble for portability.

## [0.2.0] — 2026-05-08

### Added

- **Phase 3**: `MeshClient` async wrapper around mesh-gateway HTTP API + recipient validation via `swarph_shared.validate_node_name` + best-effort credential leak guard.

## [0.1.0] — 2026-05-08

### Added

- **Phase 1**: First substrate release. `LLMAdapter` Protocol + `ChatMessage` + `LLMResponse` + `SwarphCall` public surface (caller-validated, hook-wired entry-point) + `GeminiAdapter` (wraps `langgraph-genai-bridge`) + JSON-mode harness + `FileAttributionWriter`.

## [0.0.1] — 2026-05-08

### Added

- Typed substrate scaffold: Protocol + dataclasses + exceptions.
