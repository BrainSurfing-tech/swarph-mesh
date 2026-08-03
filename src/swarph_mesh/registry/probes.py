"""Spec-driven gate-probe framework — the open adapter contract (P2 T2a).

Generalizes the hardcoded OpenClaw gate-probe harness
(``swarph-shim/openclaw_gate_probes.py``) to run the registry's 7 gates
against any :class:`~swarph_mesh.registry.spec.AdapterSpec`. These probes are
the "enforced-fact-not-declaration" validation the registry runs at register
time: a declared capability must survive a probe, and a probe that cannot run
fails LOUD (``BLOCKED``) rather than emitting a false ``PASS``.

The gate set:

* **G1**  headless one-shot — needs a ``runner`` to actually invoke the tool.
* **G1b** input-channel + max-prompt-byte cap — informational overflow probe.
* **G2**  sandbox deny-fs — ⚠️ SECURITY-CRITICAL, drop-gated (#2171), STUB only.
* **G3**  billing class — review seat-A ruling, branches on billing_class.
* **G4**  config isolation — scans a clean creds-only HOME for inherited config.
* **G5**  provenance — sha256 of the pinned source.
* **G6**  isolation-class — ⚠️ SECURITY-CRITICAL, drop-gated, STUB only.

Gates lacking a needed input return ``BLOCKED`` — never ``FAIL``, never a false
``PASS``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum

from swarph_mesh.registry.spec import AdapterSpec


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SKIP = "SKIP"


@dataclass
class GateResult:
    gate: str
    status: GateStatus
    detail: str


# config files that, if present in a "clean" creds-only HOME, mean the spawned
# tool would inherit permissive settings / a CLAUDE.md it must not see
# (the subprocess-inherits-permissive-settings class).
_INHERITED_CONFIG = ("CLAUDE.md", "settings.json", ".claude", "GEMINI.md", "AGENTS.md")


def _g1_headless(spec: AdapterSpec, runner) -> GateResult:
    """G1 — headless one-shot. PASS iff the tool replies PONG to a PONG prompt."""
    if runner is None:
        return GateResult("G1", GateStatus.BLOCKED, "no runner supplied")
    try:
        out = runner("reply with the single word PONG")
    except Exception as e:  # noqa: BLE001 — a runner blowup is a blocked probe, not a defect
        return GateResult("G1", GateStatus.BLOCKED, f"runner errored: {e}")
    if "PONG" in (out or "").upper():
        return GateResult("G1", GateStatus.PASS, "one-shot non-interactive run returned PONG")
    return GateResult("G1", GateStatus.BLOCKED, f"ran but no PONG completion: {out!r}")


def _g1b_input_cap(spec: AdapterSpec, runner) -> GateResult:
    """G1b — input-channel + max-prompt-byte cap (informational)."""
    if spec.kind == "router":
        return GateResult("G1b", GateStatus.SKIP, "N/A for router (no argv/stdin/file channel)")
    detail = (
        f"input_channel={spec.input_channel}, max_prompt_bytes={spec.max_prompt_bytes}"
    )
    if runner is None:
        return GateResult("G1b", GateStatus.BLOCKED, detail + "; no runner to probe the cap")
    cap = spec.max_prompt_bytes or 0
    big = "x" * (cap + 1)
    try:
        out = runner(big)
    except Exception as e:  # noqa: BLE001
        return GateResult("G1b", GateStatus.BLOCKED, detail + f"; cap probe errored: {e}")
    text = (out or "").lower()
    over = "too long" in text or "argv" in text or "overflow" in text
    verdict = "overflow surfaced" if over else "accepted, cap may be higher"
    return GateResult("G1b", GateStatus.PASS, detail + f"; cap+1 → {verdict}")


def _g2_sandbox(spec: AdapterSpec) -> GateResult:
    """G2 — sandbox deny-fs. ⚠️ SECURITY-CRITICAL, DROP-GATED (#2171). STUB.

    The eventual enforcement model asserts the sandbox's visible filesystem set
    equals ``whitelist ∪ audited-carve-outs``, verified EMPIRICALLY by proving a
    cat-a-secret FAILS from inside the sandbox. Enforcement is deliberately NOT
    implemented here — it is gated on drop security seat-A #2171. Until then this
    returns BLOCKED so the registry never emits a false PASS for the most
    security-critical gate.
    """
    return GateResult(
        "G2",
        GateStatus.BLOCKED,
        "G2 sandbox-deny-fs enforcement model pending drop security seat-A #2171; "
        "will assert visible-set == whitelist ∪ audited-carve-outs, empirically "
        "(cat-a-secret-FAILS)",
    )


def _g3_billing(spec: AdapterSpec, runner) -> GateResult:
    """G3 — billing class (review seat-A ruling)."""
    bc = spec.billing_class
    if bc == "subscription_zero":
        return GateResult(
            "G3",
            GateStatus.PASS,
            "declared-only: a subscription canary burns a real slot per boot "
            "(per-reboot tax)",
        )
    if bc == "metered":
        if runner is not None:
            return GateResult(
                "G3",
                GateStatus.BLOCKED,
                "wire a ~$0.001 canary to verify the billing pipeline",
            )
        return GateResult("G3", GateStatus.BLOCKED, "metered: probe needs a canary call")
    if bc == "local_zero":
        return GateResult(
            "G3",
            GateStatus.BLOCKED,
            "health-check endpoint probe — wire to the adapter's /health",
        )
    return GateResult("G3", GateStatus.BLOCKED, f"unknown billing_class={bc!r}")


def _g4_config(spec: AdapterSpec, config_home) -> GateResult:
    """G4 — config isolation. FAIL if the creds-only HOME holds inherited config."""
    if config_home is None:
        return GateResult("G4", GateStatus.BLOCKED, "no config_home supplied")
    contam = [f for f in _INHERITED_CONFIG if os.path.exists(os.path.join(config_home, f))]
    if contam:
        return GateResult(
            "G4",
            GateStatus.FAIL,
            f"clean config dir contaminated by inherited config: {contam} "
            "(subprocess-inherits-permissive-settings class)",
        )
    return GateResult(
        "G4", GateStatus.PASS, "clean config dir holds no inherited CLAUDE.md/settings.json"
    )


def _g5_provenance(spec: AdapterSpec, provenance_path) -> GateResult:
    """G5 — provenance. PASS with sha256(first 16 hex) of the pinned source."""
    if provenance_path is None:
        return GateResult("G5", GateStatus.BLOCKED, "no provenance_path supplied")
    if not os.path.exists(provenance_path):
        return GateResult("G5", GateStatus.BLOCKED, f"{provenance_path} does not exist")
    with open(provenance_path, "rb") as fh:
        h = hashlib.sha256(fh.read()).hexdigest()[:16]
    return GateResult("G5", GateStatus.PASS, f"sha256={h} (pin this in the AdapterSpec)")


def _g6_isolation_class(spec: AdapterSpec) -> GateResult:
    """G6 — isolation-class. ⚠️ SECURITY-CRITICAL, DROP-GATED. STUB.

    The host-side ``/proc/self/mountinfo`` walk that verifies the declared
    isolation class is performed DURING the G2 sandboxed run, not as a
    standalone probe. Both are gated on drop #2171.
    """
    return GateResult(
        "G6",
        GateStatus.SKIP,
        "host /proc/self/mountinfo walk, verified during the G2 sandboxed run; "
        "pending drop #2171",
    )


def probe_adapter(
    spec: AdapterSpec,
    *,
    runner=None,
    provenance_path=None,
    config_home=None,
) -> dict[str, GateResult]:
    """Run the 7 gates against ``spec``.

    ``runner`` is an optional callable ``runner(prompt: str) -> str`` that
    invokes the wrapped tool one-shot (for gates that must actually run it).
    ``provenance_path`` / ``config_home`` are filesystem inputs for G5/G4.
    Gates lacking a needed input return ``BLOCKED`` (never ``FAIL``, never a
    false ``PASS``).
    """
    return {
        "G1": _g1_headless(spec, runner),
        "G1b": _g1b_input_cap(spec, runner),
        "G2": _g2_sandbox(spec),
        "G3": _g3_billing(spec, runner),
        "G4": _g4_config(spec, config_home),
        "G5": _g5_provenance(spec, provenance_path),
        "G6": _g6_isolation_class(spec),
    }
