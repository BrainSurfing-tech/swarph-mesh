# Security Policy

`swarph-mesh` is the graph-protocol substrate that third-party LLM CLIs (and future enterprise tooling) plug into. The `LLMAdapter` Protocol, `MeshClient` wire format, and `discovery` primitives are contracts other code depends on. Reporting and triage of vulnerabilities in those contracts is taken seriously.

## Supported versions

We provide security fixes for the latest minor of the most recent two major versions. Older versions receive security fixes only for issues with no upgrade path.

| Version | Supported |
|---|---|
| 0.7.x | ✅ active |
| 0.6.x | ✅ security-only until 2026-08-09 (3 months from 0.7.0 release) |
| 0.5.x and older | ❌ end-of-life |

When a new minor (e.g., 0.8.0) ships, the previously-active minor enters security-only support for 3 months.

## What counts as a security issue

A vulnerability is in scope if any of the following:

- **Credential leakage** — paths where API keys, admin/management keys, mesh-gateway tokens, or peer credentials could surface in logs, attribution rows, exception messages, mesh DM contents, or PyPI artifacts. The `MeshClient.send` secret-guard is best-effort; bypasses are in scope.
- **Privilege boundary bypass** — the swarph contract distinguishes regular API keys from admin/management keys (OpenAI admin, xAI management) and from subscription credentials (Claude). A path where regular-key auth grants admin-key privileges, or where subscription credentials get exfiltrated to a metered billing path, is critical.
- **Cost over-attribution** — a `PRICING` table entry that systematically over-attributes by a factor (the gpt-5 5x and o3 5x classes from v0.6.x). At enterprise spend levels this becomes a financial-correctness issue.
- **Cost under-attribution** — a path that lets a peer escape attribution entirely (PRICING entry routes to `_default = 0`, attribution writer fails silently, etc.). Prevents audit and quota enforcement at scale.
- **Mesh-gateway authentication bypass** — paths where unauthenticated or wrong-token requests succeed against `/peers`, `/messages`, `/tasks/claim`, or any `mesh-gateway` endpoint.
- **Ratification bypass** — `task_claim` succeeding for a `ratified=false` peer, or the `peer_ratifications` audit log being mutable post-write.
- **Protocol contract violation** — a change to `LLMAdapter` Protocol surface, `MeshMessage` shape, `MeshClient.send` parameters, or `discovery` public API that ships without a deprecation window per `DEPRECATIONS.md` (forthcoming v0.7.2).
- **Catalog poisoning** — paths where AIMLAPI catalog data or per-provider `/v1/models` responses can inject crafted `ModelInfo` records that break adapter dispatch or mis-attribute cost.
- **DM contagion** — paths where wrong-name DMs route to the wrong peer (the a peer-name-alias drift class (e.g. `db` vs `database-node`)), or where ratified-peer status leaks across mesh boundaries before federation.
- **Substrate dependency CVE** — known CVE in `httpx`, `pydantic`, `openai`, `langgraph-genai-bridge`, or `swarph-shared` that swarph-mesh exposes to consumers.

A vulnerability is **out of scope** if:

- It requires write access to `~/.swarph/secrets.toml` or any `.env` file the operator already controls (the operator's filesystem is trusted).
- It requires execution of arbitrary code on the operator's machine (already game-over).
- It's in a third-party LLM provider's API itself rather than swarph-mesh's interface to that API.
- It's purely a denial-of-service via prompt-engineering (e.g., long inputs causing high token cost). Cost-control is the operator's concern, not a security boundary.

## How to report

**Do NOT report security issues via public GitHub issues, public PRs, or mesh DM kind=question/answer (which are logged in `claude_messages` forever).**

Reporting channels:

1. **Email**: `pierresamson@gmail.com` with subject prefix `[swarph-mesh security]`. PGP key available on request.
2. **GitHub Security Advisory** (preferred for repo-scoped findings): https://github.com/darw007d/swarph-mesh/security/advisories/new — private to the maintainer + collaborators until coordinated disclosure.

Please include:

- swarph-mesh version (`python -c "import swarph_mesh; print(swarph_mesh.__version__)"`)
- Affected component (adapter / mesh_client / discovery / etc.)
- Reproduction steps or PoC code
- Suspected severity (your read; we'll triage)
- Whether you'd like CVE assignment (we'll request via GitHub if warranted)

## Response timeline

| Stage | Target |
|---|---|
| Acknowledge receipt | 48 hours |
| Initial triage + severity assessment | 5 business days |
| Fix development | 14 days for high/critical, 30 days for medium, 90 days for low |
| Coordinated disclosure | After fix is published; reporter named in CHANGELOG unless they request anonymity |

For critical issues affecting active billing paths or credential leakage, we'll prioritize fix-and-publish over the standard cadence.

## What you'll get from us

- Acknowledgment of receipt within 48 hours
- Public credit in `CHANGELOG.md` (with your preferred name/handle, or anonymous)
- Coordinated disclosure timeline — we won't publish before you've had time to verify the fix
- For critical findings, optional inclusion in the project's "thanks" section in `README.md`

## What we ask in return

- **Don't disclose publicly until we've shipped a fix and the supported minor versions have published patches.**
- **Don't test against third parties' production swarph deployments.** Use your own mesh + your own keys.
- **Don't exfiltrate or persist credentials or peer data** even if you find a path to. Demonstrate the vulnerability against your own test harness.

## Disclosure history

This file ships in v0.7.1 — no prior security disclosures on record. Future entries:

```
v0.X.Y — YYYY-MM-DD — CVE-YYYY-NNNN — short description (severity) — credit
```

## Hardening commitments

For each minor release, we commit to at least one of:

- Tightening the secret-guard regex set (`swarph_mesh.mesh_client._CRED_PATTERNS`)
- Expanding the protocol-stability snapshot test set (`tests/test_protocol_contract.py`)
- Ratcheting one additional module under `mypy --strict` per `DEPRECATIONS.md`
- Adding one privilege-boundary test to the canonical case suite

The graph protocol is third-party-implementer-facing infrastructure. Treating each release as an opportunity to harden the contract is part of the foundations-sprint discipline.

## Federation forward-compat

When mesh-of-meshes federation lands (v0.8+ per the architectural design currently in a parallel session), this policy will be updated to cover:

- Cross-mesh DM authentication & non-repudiation
- Federation registry trust roots
- Cross-mesh ratification handshakes
- Cross-mesh credential isolation

Until federation lands, mesh-to-mesh interaction is out of scope; one mesh = one Tailnet (or equivalent VPN) = one trust boundary.
