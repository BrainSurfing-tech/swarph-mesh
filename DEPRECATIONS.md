# Versioning Policy & Deprecations Tracker

This document defines what counts as a breaking change in `swarph-mesh`, the deprecation cycle policy, and the live tracker of deprecated surfaces.

## Why this document exists

`swarph-mesh` is the **graph protocol substrate** that third-party CLIs and future enterprise tooling plug into via the `LLMAdapter` Protocol, `MeshClient` wire format, and `discovery` primitives. These contracts are external-facing — silent changes orphan node implementers.

This file makes the contract explicit:

- What surfaces are part of the public contract
- What constitutes a breaking change to each
- How long deprecation must run before removal
- The current live deprecations and their removal timelines

Pairs with [SECURITY.md](./SECURITY.md), which lists "Protocol contract violation" as a reportable security class — meaning shipping a breaking change without honoring the policy in this document is itself a security incident.

## Semantic versioning policy

`swarph-mesh` follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) with one tightening:

- **MAJOR (X.0.0)**: any backwards-incompatible change to the public contract surface.
- **MINOR (0.X.0)**: backwards-compatible additions to public contract; can include backwards-compatible deprecations announced for removal in a future MAJOR.
- **PATCH (0.0.X)**: backwards-compatible bug fixes only. **NO** deprecation announcements, **NO** new public API.

We're currently in `0.x` zone where some loosening is allowed: `0.MINOR.PATCH` may add API in MINOR, fix bugs in PATCH. `LLMAdapter` Protocol changes still require deprecation cycle even pre-1.0 because external node implementers exist.

When `1.0.0` ships, all rules tighten to standard semver.

## What counts as the public contract

Each module/symbol below is part of the contract. Changes to these require deprecation cycle.

### Tier 1 (HIGHEST stability — third-party node-implementer-facing)

- **`swarph_mesh.types.LLMAdapter`** Protocol — methods, signatures, kwarg defaults, return types
- **`swarph_mesh.types.ChatMessage`** + **`LLMResponse`** field sets
- **Canonical adapter names**: `{gemini, deepseek, claude, openai, grok}` reserved; cannot be renamed without major version bump
- **`MeshClient.send`** + **`MeshClient.fetch`** signatures (third-party callers depend on the kwarg shape)
- **`MeshMessage`** wire shape (claude_messages JSONL persistence depends on this)
- **`discovery.list_models`** / **`is_model_supported`** / **`get_model_info`** signatures
- **`discovery.fetch_*_pricing`** + **`fetch_*_cost_buckets`** + **`reconcile_*_cost`** signatures

### Tier 2 (MEDIUM stability — consumer-facing but internal-friendly)

- **`SwarphCall`** public methods + kwargs
- **`AttributionWriter`** Protocol + `FileAttributionWriter` / `NullAttributionWriter` defaults
- **`HookSet`** + **`CallContext`** dataclasses
- **`discovery.normalize_*_id`** helper signatures (drop DM #745 obs #1)
- **`discovery._RETIREMENT_REGISTRY`** keys (consumers may iterate)

### Tier 3 (LOWER stability — best-effort but documented)

- Adapter-local `PRICING` dicts (consumers should call `cost_per_token()` instead of importing PRICING directly, but some do — flagged in `SECURITY.md` as cost-attribution concern)
- `_normalize_*_id` adapter-local copies (preserved for back-compat per drop #745; eventual removal once centralized helpers settle)
- `discovery._ANTHROPIC_PRICING` static table contents

### NOT part of the contract (no stability guarantee)

- Module-level private `_*` symbols (registry caches, helpers)
- Test fixtures, internal utility functions
- Mesh-gateway database schema (separate repo, separate semver)
- swarph-shared interface (separate package; tracked in its own DEPRECATIONS)

## What counts as a breaking change

For each Tier 1/2 surface:

### LLMAdapter Protocol

- **Adding** a method → BREAKING (third-party implementations now fail `isinstance(x, LLMAdapter)`). Required deprecation: announce in MINOR, ship with default no-op or fallback behavior, remove `isinstance` requirement enforcement until next MAJOR.
- **Removing** a method → BREAKING. Required deprecation: announce removal in MINOR-1, log `DeprecationWarning` for 1 minor cycle, remove in next MINOR.
- **Renaming** a method → ALWAYS BREAKING. Treat as add+remove with deprecation cycle on the old name.
- **Changing a method signature** (kwarg removal, kwarg-default change, return-type narrowing) → BREAKING. Add new method, deprecate old, remove old in next MINOR cycle.
- **Adding a method with default behavior** (e.g., `list_models()` in v0.6.0 with discovery delegation) → STILL BREAKING under runtime-checkable Protocol semantics, but minimally so. Treated as MINOR with deprecation announcement that mocks/stubs need updating.

### MeshMessage / MeshClient wire format

- **Adding** a field with default → NON-BREAKING (extra: allow per pydantic config).
- **Removing** a field → BREAKING. Deprecation cycle: 1 minor of `DeprecationWarning` on `model_dump()`, then remove.
- **Making an optional field required** → BREAKING. Same cycle.
- **Renaming** a field → ALWAYS BREAKING. Add new + deprecate old + remove old in next MINOR.

### Canonical adapter names

- **Renaming** a canonical name (e.g., `gemini` → `google`) → MAJOR-version-only change. No deprecation path that doesn't break existing string-pinned consumers.
- **Adding** a new canonical name → NON-BREAKING.
- **Removing** a canonical name (deprecating a provider entirely) → MINOR with `DeprecationWarning` for 2 cycles, then MAJOR.

## Deprecation cycle requirements

1. **Announcement**: PR introducing the deprecation must update this file's "Active deprecations" section AND add a `DeprecationWarning` in the relevant code path.
2. **Cycle length**: minimum 1 MINOR release where the old surface emits warnings AND continues to work.
3. **Removal**: in the MINOR after the announcement (or later — never sooner).
4. **CHANGELOG entry**: every deprecation announcement and every removal lands in `CHANGELOG.md`.
5. **Migration guide**: deprecations affecting Tier 1 surfaces require a migration paragraph in this file.

## Active deprecations

_None at v0.7.2._

When a deprecation lands, this section gains an entry like:

```
### deprecated_method_name (since v0.X.Y, removal in v0.X+1.0)

**What:** Old surface description.

**Why:** Reason for deprecation (replaced by Z, security tightening, etc.).

**Migration:** Concrete code example old→new.

**Status:** Announced 2026-MM-DD. DeprecationWarning live since v0.X.Y.
Removal scheduled for v0.X+1.0.
```

## Removed surfaces (historical)

_None — `swarph-mesh` has not yet removed any public surface._

When a removal happens, this section records it for archaeology:

```
### method_name (removed in v0.X.0)

**Was:** Old surface description.

**Replaced by:** New surface, if any.

**Final deprecation cycle:** v0.Y.Z → v0.X.0.
```

## Forward-references in v0.7.0 / v0.7.1 (resolved by this document)

- `SECURITY.md` § 7 (Protocol contract violation): "shipped without a deprecation window per `DEPRECATIONS.md`" — **policy now lives here**.
- `tests/test_protocol_contract.py` test failure messages: "If intentional, update `PROTOCOL_FROZEN_AT_VERSION` + add entry to `DEPRECATIONS.md`" — **the entry format is "Active deprecations" above**.
- CI workflow `mypy --strict` ratchet comment: "each minor version tightens one additional module per the `DEPRECATIONS.md` ratcheting discipline" — **ratchet schedule documented in next section**.

## Mypy --strict module ratchet schedule

The CI gate (`.github/workflows/ci.yml`) currently runs `mypy --strict` on `types.py` + `exceptions.py`. Each subsequent minor tightens one more module:

| Version | Module added to strict gate | Status |
|---|---|---|
| v0.7.0 | `types.py`, `exceptions.py` | ✅ initial |
| v0.7.1 | (none — security policy release) | ✅ |
| v0.7.2 | (none — versioning policy release) | ✅ |
| v0.7.3 | (none — Sphinx docs release; doc surface itself is the hardening) | ✅ |
| v0.7.4 | `discovery.py` (after type-tightening pass) | planned |
| v0.7.5 | `__init__.py` + `mesh_client.py` | planned |
| v0.7.6 | `swarph_call.py` + `attribution.py` + `hooks.py` | planned |
| v0.8.0 | `adapters/*.py` (per-adapter strict in major bump window) | planned |

When a module is added to the strict gate, the corresponding type-tightening (e.g., `dict` → `dict[str, Any]`) is part of that release's PR, NOT a follow-up. Tightening type annotations is non-breaking (more specific → not less specific).

## Hardening commitment per release (anchor point)

`SECURITY.md` commits to "each minor release tightens at least one of: secret-guard regexes, protocol-stability snapshots, mypy strict-module ratchet, privilege-boundary tests."

Tracked here:

| Version | Hardening dimension | What |
|---|---|---|
| v0.7.0 | protocol-stability + mypy | Initial 10 snapshot tests + types.py/exceptions.py strict gate |
| v0.7.1 | secret-guard | (none — governance release; v0.7.4 includes regex audit) |
| v0.7.2 | governance | DEPRECATIONS.md anchors policy referenced by SECURITY.md |
| v0.7.3 | doc-surface | Sphinx auto-docs + GH Pages strict-mode build forces autodoc miss = build break, keeping documented contract in lockstep with code |
| v0.7.4 | coverage-surface | Codecov patch coverage gate at 70% — fresh uncovered code blocks PR merge; project regression tolerance 1%; baseline 90% line / 88% branch on 1316 statements |
| v0.7.5+ | TBD per release | tracked here |

## How to file a deprecation

1. Open a PR adding entry to "Active deprecations" above.
2. Code change emits `warnings.warn("...", DeprecationWarning, stacklevel=2)` at the deprecated callsite.
3. PR description names the removal target version.
4. CHANGELOG.md "Unreleased" gets the announcement entry.
5. After the announcement ships, the removal PR (in a later release) moves the entry from "Active deprecations" to "Removed surfaces" in this file + removes the code.

## When in doubt

If you're unsure whether a change is breaking, the default is YES. Open a PR with a deprecation cycle. The cost of an unnecessary deprecation cycle is one minor release of warnings; the cost of a silent breaking change is third-party node implementers losing trust in the contract.

That trust is the load-bearing thing. Protect it.
