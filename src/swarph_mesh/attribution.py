"""Attribution event + writer protocol.

Per PLAN.md §8, every ``SwarphCall`` writes one row of attribution
data after a successful adapter call. The package ships writer
implementations for common cases (no-op, JSONL file); production
consumers (OMEGA droplet) wire up a TSDB writer at module load.

The writer is a Protocol — consumers can plug in any callable
matching the shape. Default in v0.1.0: ``FileAttributionWriter``
appending to ``~/.swarph/attribution.jsonl`` so cost data isn't
silently dropped on first run, but no DB driver is forced.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class AttributionEvent:
    """One row of cost / latency / token attribution for an LLM call.

    Fields match the shape consumed by the OMEGA ``token_usage`` /
    ``subscription_usage`` TimescaleDB hypertables (see
    hedge-fund-mcp ``database/init_timescale.sql``). External
    consumers can map to their own schema in their writer impl.
    """

    timestamp: float                 # unix seconds at call completion
    provider: str                    # "gemini" | "claude" | ...
    model: str                       # provider-specific model id
    role: str                        # caller role label (orchestrator/council/...)
    caller: str                      # dotted-slug per swarph_shared.caller_convention
    mesh_peer: Optional[str]         # peer scope for attribution joins, may be None
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    cached: bool = False
    error_class: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class AttributionWriter(Protocol):
    """Protocol for sink-side attribution writers. Implementations
    can write to TSDB, JSONL files, OTLP, stdout, or be no-op."""

    async def write(self, event: AttributionEvent) -> None: ...


class NullAttributionWriter:
    """No-op. Useful in test fixtures where attribution noise is
    distracting and the call's `LLMResponse` already carries the
    cost/token data inline."""

    async def write(self, event: AttributionEvent) -> None:  # noqa: D401
        return None


class FileAttributionWriter:
    """Append-only JSONL writer at ``~/.swarph/attribution.jsonl``.

    Default writer in v0.1.0 — every call lands a parseable line,
    no DB driver required. Single-process safe via an asyncio.Lock.
    Multi-process scenarios (which v0.1.0 doesn't target) would
    need a file-lock; document and defer.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / ".swarph" / "attribution.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def write(self, event: AttributionEvent) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        async with self._lock:
            # blocking write inside the lock — small, line-buffered
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)


_default_writer: AttributionWriter = FileAttributionWriter()


def get_default_writer() -> AttributionWriter:
    """Return the module-level default writer. ``SwarphCall`` uses
    this when no per-call writer is provided."""
    return _default_writer


def set_default_writer(writer: AttributionWriter) -> None:
    """Override the module-level default writer (e.g., production
    consumers call this at startup with a TSDB-backed writer)."""
    global _default_writer
    _default_writer = writer


def make_event(
    *,
    provider: str,
    model: str,
    role: str,
    caller: str,
    mesh_peer: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_s: float = 0.0,
    cached: bool = False,
    error_class: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> AttributionEvent:
    """Construct an :class:`AttributionEvent` with ``timestamp=now``."""
    return AttributionEvent(
        timestamp=time.time(),
        provider=provider,
        model=model,
        role=role,
        caller=caller,
        mesh_peer=mesh_peer,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
        duration_s=duration_s,
        cached=cached,
        error_class=error_class,
        extra=extra or {},
    )
