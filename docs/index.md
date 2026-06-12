# swarph-mesh

**Model-agnostic Python substrate for the swarph-mesh ecosystem — the graph protocol third-party CLIs plug into.**

`swarph-mesh` is the bottom layer of a four-package ecosystem:

| Package | Role |
|---|---|
| [`swarph-shared`](https://pypi.org/project/swarph-shared/) | substrate primitives shared across all swarph packages (caller convention, subprocess env scrubbing, JSON mode, peer registry) |
| **`swarph-mesh`** (this package) | typed Protocol + adapters + SwarphCall + MeshClient + discovery |
| [`swarph-cli`](https://pypi.org/project/swarph-cli/) | `swarph` CLI binary; thin wrapper over `swarph-mesh` |
| [`swarph-meshlm`](https://pypi.org/project/swarph-meshlm/) | Simon Willison `llm` plugin |

## What this documentation covers

This site auto-renders the public contract surface of `swarph-mesh`. The contract is enumerated in [`DEPRECATIONS.md`](https://github.com/BrainSurfing-tech/swarph-mesh/blob/main/DEPRECATIONS.md) — Tier 1 / Tier 2 / Tier 3.

- **Tier 1** — third-party-node-implementer-facing primitives. Highest stability. `LLMAdapter` Protocol, `ChatMessage`, `LLMResponse`, `MeshClient`, `MeshMessage`, discovery primitives. Changes here trigger the deprecation cycle in `DEPRECATIONS.md`.
- **Tier 2** — consumer-facing-but-internal-friendly. `SwarphCall`, `AttributionWriter`, `HookSet`, normalization helpers.
- **Tier 3** — best-effort-documented. Adapter-local `PRICING` dicts, internal pricing tables.
- **Not part of the contract** — module-level private `_*` symbols, test fixtures, mesh-gateway database schema (separate repo).

If a symbol is not visible in these auto-generated pages, it is not part of the contract.

## Stability commitments

Three documents anchor the contract surface:

- [`SECURITY.md`](https://github.com/BrainSurfing-tech/swarph-mesh/blob/main/SECURITY.md) — 9 in-scope vulnerability classes; "Protocol contract violation" is reportable as a security event.
- [`DEPRECATIONS.md`](https://github.com/BrainSurfing-tech/swarph-mesh/blob/main/DEPRECATIONS.md) — what counts as breaking, deprecation cycle requirements, mypy-strict ratchet schedule.
- [`CHANGELOG.md`](https://github.com/BrainSurfing-tech/swarph-mesh/blob/main/CHANGELOG.md) — full per-release history.

```{toctree}
:maxdepth: 2
:caption: API reference

api/types
api/adapters
api/swarph_call
api/mesh_client
api/discovery
api/attribution
api/hooks
api/exceptions
```

```{toctree}
:maxdepth: 1
:caption: Project

changelog
deprecations
security
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
