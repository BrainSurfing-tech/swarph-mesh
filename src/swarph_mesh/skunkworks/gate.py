"""Architect verdict gate: a commander APPROVED is refused unless the architect said
APPROVED_FOR_BUILD. There is no override: the only ways past a negative audit are a
new run or a REVISE/REJECTED decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_APPROVED_VERDICT = "APPROVED_FOR_BUILD"


@dataclass
class GateRefusal:
    arch_verdict: str
    message: str

    def to_dict(self) -> dict:
        return {
            "error": "architect_verdict_gate",
            "arch_verdict": self.arch_verdict,
            "message": self.message,
        }


def check_architect_verdict_gate(
    *, commander_verdict: str, arch_verdict: Optional[str]
) -> Optional[GateRefusal]:
    """Return a GateRefusal if APPROVED conflicts with the architect's verdict, else None.

    A missing or empty architect verdict also blocks APPROVED (fail-safe).
    """
    cv = (commander_verdict or "").strip().upper()
    av = (arch_verdict or "").strip().upper()
    if cv != "APPROVED" or av == _APPROVED_VERDICT:
        return None
    return GateRefusal(
        arch_verdict=av or "MISSING",
        message=(
            f"architect verdict was {av or 'MISSING'!r}, not {_APPROVED_VERDICT}. "
            "APPROVED refused; use REVISE or REJECTED, or start a new run."
        ),
    )
