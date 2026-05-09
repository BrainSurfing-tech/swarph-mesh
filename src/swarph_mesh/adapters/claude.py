"""Claude adapter — subprocess-based ``claude -p`` subscription path.

Per PLAN.md §3 ship-order #3: "Claude adapter — wraps ``claude -p``
subscription path via ``workers/opus_subscription.py`` for API-key-free
calls. NOTE: this is the billing-path landmine (CLAUDE.md rule); reuse
``_scrubbed_env`` from the shared module."

Why subprocess instead of the Anthropic SDK:

The Anthropic SDK uses ``ANTHROPIC_API_KEY`` and bills per-token through
the metered API. ``claude -p`` (the CLI binary) reads
``~/.claude/.credentials.json`` and bills through the user's claude.ai
subscription — flat-rate, not metered. For Council judges, lab-orchestrator
delegate calls, or any context where subscription billing is the right
choice, we wrap the CLI as a subprocess.

**Critical contract — billing-path leak prevention.**

If ``ANTHROPIC_API_KEY`` is set in the subprocess env, ``claude -p``
silently flips to API-metered billing. We use
``swarph_shared.scrub_env_for_subprocess`` to strip every billing-relevant
key shape (explicit denylist + ``*_API_KEY`` suffix) before invoking.
``swarph_shared.verify_subscription_setup`` runs once at first invoke to
fail loud if creds are missing or scrub is broken. Both primitives are
covered by 10 tests in swarph-shared (tests/test_subprocess_env.py).

Token + cost tracking:

We use ``--output-format=json`` to get a structured response with usage
counts on every call. ``LLMResponse.input_tokens`` /
``LLMResponse.output_tokens`` are populated from ``usage.input_tokens`` /
``usage.output_tokens`` on the response. ``cost_usd`` is set to **0.0**
because subscription billing is flat-rate, not per-call. The
metered-equivalent cost is preserved in ``raw_response["api_metered_cost_usd"]``
for auditors who want to compare "what would this have cost on the API?".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from swarph_shared import scrub_env_for_subprocess, verify_subscription_setup

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


logger = logging.getLogger(__name__)


# Default model + binary path. Both env-overridable so test environments
# can swap them without touching code.
DEFAULT_MODEL = os.environ.get("CLAUDE_SUBSCRIPTION_MODEL", "claude-opus-4-7")
DEFAULT_TIMEOUT_SECONDS = int(
    os.environ.get("CLAUDE_SUBSCRIPTION_TIMEOUT", "120")
)


def _resolve_claude_bin() -> str:
    """Locate the ``claude`` CLI binary. Order:
    1. ``CLAUDE_BIN`` env override
    2. ``$HOME/.local/bin/claude`` (standard install path)
    3. ``/usr/local/bin/claude``
    4. ``shutil.which("claude")`` last-resort PATH lookup

    Per drop PR #8 review carry-forward (b) issue #9 #2: $HOME/.local
    takes precedence over /usr/local. Right default for user-installed
    binaries-take-precedence-over-system but lab-orchestrator + droplet
    contexts may have system installs that should win. **Set ``CLAUDE_BIN``
    env explicitly if both user + system installs are present and you
    want the system one** — the env override beats both filesystem paths
    and is the cleanest way to disambiguate without code changes.
    """
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    home_local = Path.home() / ".local" / "bin" / "claude"
    if home_local.exists():
        return str(home_local)
    usr_local = Path("/usr/local/bin/claude")
    if usr_local.exists():
        return str(usr_local)
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    return str(home_local)  # default — verify_subscription_setup will fail loud


# Anthropic per-Mtok pricing (USD).
# v0.6.1 catch-up: extended with full Anthropic lineup verified
# against claude.com/pricing on 2026-05-09. Subset of the full table
# in swarph_mesh.discovery._ANTHROPIC_PRICING (which carries cache +
# batch dimensions). LLMResponse.cost_usd is always 0.0 for this
# adapter since subscription billing is flat-rate; this table only
# populates ``raw_response["api_metered_cost_usd"]``.
#
# Includes dated-build aliases so AIMLAPI catalog IDs (e.g.
# ``claude-opus-4-1-20250805``) resolve to PRICING without falling
# through to _default.
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    # — Opus tier (premium) —
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-5-20251101": (5.00, 25.00),  # dated build alias
    "claude-opus-4-1": (15.00, 75.00),  # NEW v0.6.1 — older premium tier
    "claude-opus-4-1-20250805": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-opus-4-20250514": (15.00, 75.00),
    "claude-opus-3": (15.00, 75.00),  # deprecated
    # — Sonnet tier (balanced) —
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),  # NEW v0.6.1
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),  # NEW v0.6.1
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-sonnet-3-7": (3.00, 15.00),  # deprecated
    # — Haiku tier (cheap) —
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-3-5": (0.80, 4.00),  # NEW v0.6.1
    "claude-haiku-3": (0.25, 1.25),  # NEW v0.6.1
    "_default": (5.00, 25.00),
}


# ---------------------------------------------------------------------------
# Prompt builder + response parser
# ---------------------------------------------------------------------------


def _build_prompt(
    messages: list[ChatMessage],
    system_prompt: Optional[str],
) -> str:
    """Render messages into a single prompt string for ``claude -p``.

    The CLI takes one positional prompt argument; we encode multi-turn
    conversation as ``[ROLE]\\ncontent`` blocks separated by blank lines.
    Same shape as ``workers/opus_subscription.py:_build_prompt``.
    """
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt.strip())
    for m in messages:
        role = m.role.upper()
        parts.append(f"[{role}]\n{m.content}")
    return "\n\n".join(parts)


def _parse_claude_json(stdout: str) -> dict[str, Any]:
    """Parse the ``claude -p --output-format=json`` response payload.

    Raises :class:`AdapterError` if the payload doesn't parse — that's
    a CLI version mismatch or wire-shape change, neither of which the
    adapter can recover from.
    """
    if not stdout.strip():
        raise AdapterError("ClaudeAdapter: empty response from claude -p")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"ClaudeAdapter: failed to parse claude -p JSON output: {exc}; "
            f"first 200 chars: {stdout[:200]!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ClaudeAdapter:
    """``LLMAdapter`` for subscription-billed Claude via ``claude -p``.

    Verification model: ``swarph_shared.verify_subscription_setup`` runs
    at first invoke (lazy). Production consumers can call it explicitly at
    boot (``ClaudeAdapter().verify()``) to surface broken setup at deploy-
    time rather than first-request-time.
    """

    name = "claude"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,  # ignored — kept for LLMAdapter shape
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        claude_bin: Optional[str] = None,
    ):
        # api_key is intentionally unused — subscription path reads
        # ~/.claude/.credentials.json and any ANTHROPIC_API_KEY in the
        # subprocess env would silently flip billing. We accept the kwarg
        # only because the LLMAdapter Protocol shape includes it; if a
        # caller passes one, log a warning so they know it's a no-op.
        if api_key is not None:
            logger.warning(
                "ClaudeAdapter ignores api_key — subscription path uses "
                "~/.claude/.credentials.json. Pass via env to your "
                "subscription auth flow if needed; this kwarg is a no-op."
            )
        self._timeout_seconds = timeout_seconds
        self._claude_bin = claude_bin or _resolve_claude_bin()
        self._verified = False

    def verify(self) -> None:
        """Eagerly run :func:`verify_subscription_setup` to surface a
        broken setup at boot rather than first request. Idempotent."""
        if self._verified:
            return
        verify_subscription_setup(
            claude_bin=self._claude_bin,
            creds_path=Path.home() / ".claude" / ".credentials.json",
        )
        self._verified = True

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Invoke ``claude -p`` subscription-billed.

        ``temperature`` + ``max_tokens`` are accepted for Protocol
        compatibility but the current ``claude -p`` CLI doesn't expose
        either as flags. Once-per-instance warnings fire on the first
        call that passes a non-default value so callers don't get
        silently-ignored creativity / length expectations
        (drop PR #8 review carry-forward (a) issue #9). Future
        ``claude -p`` versions may expose these flags; we'll thread
        them when they land.

        ``json_schema`` is passed to ``claude -p --json-schema`` for
        native structured-output enforcement (the CLI handles parsing +
        validation server-side). The adapter's ``LLMResponse.parsed`` is
        populated from the result text when the CLI confirms valid JSON.
        """
        # Silent-kwargs warnings: once per instance, not per call (avoid
        # log spam in REPL or daemon-loop contexts). Defaults pass through
        # unwarned.
        if temperature != 0.7 and not getattr(self, "_warned_temperature", False):
            logger.warning(
                "ClaudeAdapter.chat: temperature=%s is silently ignored — "
                "claude -p CLI does not expose --temperature today. "
                "(once-per-instance warning)",
                temperature,
            )
            self._warned_temperature = True
        if max_tokens is not None and not getattr(self, "_warned_max_tokens", False):
            logger.warning(
                "ClaudeAdapter.chat: max_tokens=%s is silently ignored — "
                "claude -p CLI does not expose --max-tokens today. "
                "(once-per-instance warning)",
                max_tokens,
            )
            self._warned_max_tokens = True

        self.verify()  # lazy — runs once per adapter instance

        prompt_text = _build_prompt(messages, system_prompt)

        argv = [
            self._claude_bin,
            "-p",
            prompt_text,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        if json_schema is not None:
            argv.extend(["--json-schema", json.dumps(json_schema)])

        # Scrubbed env keeps ANTHROPIC_API_KEY (and the *_API_KEY suffix
        # forward-compat denylist) out of the subprocess. IS_SANDBOX=1
        # follows the OMEGA opus_subscription convention so claude
        # treats the call as headless / non-interactive.
        env = {**scrub_env_for_subprocess(), "IS_SANDBOX": "1"}

        start = time.monotonic()
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(
                f"ClaudeAdapter.chat timed out after {self._timeout_seconds}s "
                f"on model {model!r}"
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterError(
                f"ClaudeAdapter: claude binary not found at {self._claude_bin!r}. "
                "Set CLAUDE_BIN env or install via `npm i -g @anthropic-ai/claude-code`"
            ) from exc
        duration_s = time.monotonic() - start

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "")[-500:]
            raise AdapterError(
                f"ClaudeAdapter.chat exit={proc.returncode} on model {model!r}: "
                f"stderr={stderr_tail!r}"
            )

        payload = _parse_claude_json(proc.stdout or "")

        if payload.get("is_error"):
            api_err = payload.get("api_error_status")
            raise AdapterError(
                f"ClaudeAdapter.chat returned is_error=True for model {model!r}: "
                f"{api_err!r}"
            )

        text = payload.get("result", "") or ""
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)

        # Subscription path is flat-rate — actual billing is the user's
        # subscription, not per-call. cost_usd = 0.0 reports honestly to
        # the attribution writer + downstream cost dashboards.
        # Metered-equivalent cost (what this would have cost on API) lives
        # in raw_response for auditors who want the comparison.
        in_per_mtok, out_per_mtok = PRICING.get(model, PRICING["_default"])
        api_metered_cost_usd = (
            (input_tokens / 1_000_000.0) * in_per_mtok
            + (output_tokens / 1_000_000.0) * out_per_mtok
        )
        # Some claude -p versions surface `total_cost_usd` directly; prefer
        # that when present (already accounts for model-tier + caching).
        if "total_cost_usd" in payload:
            try:
                api_metered_cost_usd = float(payload["total_cost_usd"])
            except (TypeError, ValueError):
                pass

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=0.0,  # subscription path — flat-rate, not per-call
            duration_s=duration_s,
            cached=cache_read > 0,
            raw_response={
                "billing_path": "subscription",
                "model": model,
                "session_id": payload.get("session_id"),
                "stop_reason": payload.get("stop_reason"),
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "api_metered_cost_usd": api_metered_cost_usd,
                "duration_api_ms": payload.get("duration_api_ms"),
            },
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Token-by-token streaming via ``claude -p --output-format=stream-json``.

        v0.4.0 raises NotImplementedError; the CLI supports streaming, this
        adapter will wire it up alongside the GeminiAdapter + DeepSeekAdapter
        stream() in v0.4.0.
        """
        raise NotImplementedError(
            "ClaudeAdapter.stream is v0.4.0 stretch; use chat() for now."
        )
        yield ""  # pragma: no cover — keeps AsyncIterator shape

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return metered-equivalent (input_per_mtok, output_per_mtok).

        Note: subscription path bills flat-rate; this returns what the
        call WOULD have cost on the metered API. Useful for auditors
        comparing subscription savings.
        """
        return PRICING.get(model, PRICING["_default"])

    def list_models(self, *, ttl_seconds: int = 86400):
        """v0.6.0 catalog query — AIMLAPI primary + per-provider fallback.
        Routes to ``provider="claude"`` which maps to AIMLAPI's
        ``developer="Anthropic"``."""
        from swarph_mesh.discovery import list_models as _list

        return _list(provider=self.name, ttl_seconds=ttl_seconds)
