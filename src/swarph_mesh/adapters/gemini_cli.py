"""Gemini-CLI adapter — subprocess-based ``gemini -p`` subscription path.

The mirror of :mod:`swarph_mesh.adapters.claude` for Google. Where
``GeminiAdapter`` (gemini.py) bills the **metered** Generative-Language
API via the SDK (``GEMINI_API_KEY`` → ``GenAIBridge``), this adapter
shells out to the ``gemini`` CLI binary, which reads
``~/.gemini/oauth_creds.json`` and bills through the user's Google
subscription — flat-rate, not metered. Same shape as the Claude
subscription path.

Why a separate provider name (``"gemini-cli"``) instead of replacing
``"gemini"``: the metered SDK path stays the default for callers who
want pay-per-token + Flex rebate + Pro context caching (the bridge).
Subscription billing is opt-in via ``--provider gemini-cli`` so nobody
gets silently re-routed onto their personal Google subscription.

**Critical contract — billing-path leak prevention.**

If ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` is set in the subprocess
env, the ``gemini`` CLI flips to API-metered billing instead of the
subscription. We use ``swarph_shared.scrub_env_for_subprocess`` (the
same primitive the Claude adapter uses — explicit denylist + ``*_API_KEY``
suffix) to strip every billing-relevant key shape before invoking.
Verified: both ``GEMINI_API_KEY`` and ``GOOGLE_API_KEY`` are stripped,
``PATH`` / ``HOME`` preserved.

Token + cost tracking:

``-o json`` returns ``{"session_id", "response", "stats"}`` where
``stats.models.<model>.tokens`` carries ``{prompt, candidates, cached,
...}`` per model (the CLI may invoke a utility-router model plus the
main model). We aggregate ``prompt`` as input and ``candidates`` as
output across all models. ``cost_usd`` is **0.0** (subscription =
flat-rate); metered-equivalent lives in
``raw_response["api_metered_cost_usd"]`` for auditors.
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

from swarph_shared import scrub_env_for_subprocess

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


logger = logging.getLogger(__name__)


# Default model + binary path. Both env-overridable so test environments
# can swap them without touching code. Empty default model => let the CLI
# pick its subscription-default model (no ``-m`` flag passed).
DEFAULT_MODEL = os.environ.get("GEMINI_SUBSCRIPTION_MODEL", "")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_SUBSCRIPTION_TIMEOUT", "120"))


def _resolve_gemini_bin() -> str:
    """Locate the ``gemini`` CLI binary. Order mirrors the Claude adapter:
    1. ``GEMINI_BIN`` env override
    2. ``/usr/local/bin/gemini`` (standard npm-global install path)
    3. ``$HOME/.local/bin/gemini``
    4. ``shutil.which("gemini")`` last-resort PATH lookup

    Note the order differs from Claude: the ``gemini`` CLI installs to
    ``/usr/local/bin`` by default (npm global), so the system path wins
    unless ``GEMINI_BIN`` is set explicitly.
    """
    if os.environ.get("GEMINI_BIN"):
        return os.environ["GEMINI_BIN"]
    usr_local = Path("/usr/local/bin/gemini")
    if usr_local.exists():
        return str(usr_local)
    home_local = Path.home() / ".local" / "bin" / "gemini"
    if home_local.exists():
        return str(home_local)
    import shutil

    found = shutil.which("gemini")
    if found:
        return found
    return str(usr_local)  # default — invoke will fail loud if absent


# Google per-Mtok pricing (USD). Subscription path bills flat-rate, so
# LLMResponse.cost_usd is always 0.0; this table only populates
# ``raw_response["api_metered_cost_usd"]`` for the "what would this have
# cost on the metered API?" comparison. Kept small + with a _default;
# the metered GeminiAdapter (gemini.py) carries the authoritative table.
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "gemini-3-pro": (1.25, 5.00),
    "gemini-3-flash": (0.10, 0.40),
    "gemini-3-flash-preview": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.019, 0.075),
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.019, 0.075),
    "_default": (0.10, 0.40),
}


# ---------------------------------------------------------------------------
# Prompt builder + response parser
# ---------------------------------------------------------------------------


def _build_prompt(
    messages: list[ChatMessage],
    system_prompt: Optional[str],
) -> str:
    """Render messages into a single prompt string for ``gemini -p``.

    Same encoding as the Claude adapter: ``[ROLE]\\ncontent`` blocks
    separated by blank lines. The CLI takes one positional prompt.
    """
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt.strip())
    for m in messages:
        role = m.role.upper()
        parts.append(f"[{role}]\n{m.content}")
    return "\n\n".join(parts)


def _parse_gemini_json(stdout: str) -> dict[str, Any]:
    """Parse the ``gemini -p -o json`` response payload.

    Raises :class:`AdapterError` if the payload doesn't parse — a CLI
    version mismatch or wire-shape change the adapter can't recover from.
    """
    if not stdout.strip():
        raise AdapterError("GeminiCLIAdapter: empty response from gemini -p")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"GeminiCLIAdapter: failed to parse gemini -p JSON output: {exc}; "
            f"first 200 chars: {stdout[:200]!r}"
        ) from exc


def _aggregate_tokens(stats: dict[str, Any]) -> tuple[int, int, int]:
    """Sum (input, output, cached) tokens across every model in ``stats``.

    The CLI may invoke multiple models per call (e.g. a utility-router
    plus the main model); honest accounting sums them. ``prompt`` is the
    billed input count (includes cached); ``candidates`` is the output.
    """
    models = (stats or {}).get("models") or {}
    inp = out = cached = 0
    for m in models.values():
        tok = (m or {}).get("tokens") or {}
        inp += int(tok.get("prompt", tok.get("input", 0)) or 0)
        out += int(tok.get("candidates", 0) or 0)
        cached += int(tok.get("cached", 0) or 0)
    return inp, out, cached


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GeminiCLIAdapter:
    """``LLMAdapter`` for subscription-billed Gemini via ``gemini -p``.

    The Google counterpart to :class:`ClaudeAdapter`. Reads
    ``~/.gemini/oauth_creds.json`` (the CLI's subscription login) and bills
    flat-rate. ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` are scrubbed from
    the subprocess env to prevent a silent flip to metered billing.
    """

    name = "gemini-cli"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,  # ignored — kept for LLMAdapter shape
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        gemini_bin: Optional[str] = None,
    ):
        # api_key is intentionally unused — subscription path reads
        # ~/.gemini/oauth_creds.json and any *_API_KEY in the subprocess
        # env would silently flip billing to metered. We accept the kwarg
        # only because the LLMAdapter Protocol shape includes it.
        if api_key is not None:
            logger.warning(
                "GeminiCLIAdapter ignores api_key — subscription path uses "
                "~/.gemini/oauth_creds.json (gemini CLI OAuth login). This "
                "kwarg is a no-op; for metered billing use --provider gemini."
            )
        self._timeout_seconds = timeout_seconds
        self._gemini_bin = gemini_bin or _resolve_gemini_bin()

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Invoke ``gemini -p`` subscription-billed.

        ``temperature`` + ``max_tokens`` are accepted for Protocol
        compatibility; the ``gemini -p`` CLI doesn't expose either as a
        flag today, so they fire a once-per-instance warning when set to
        a non-default value (same discipline as the Claude adapter).

        ``json_schema`` is accepted for Protocol shape but not enforced —
        the gemini CLI has no ``--json-schema`` flag; callers needing
        native structured output should use the metered ``--provider
        gemini`` path (the SDK + GenAIBridge support response schemas).
        """
        if temperature != 0.7 and not getattr(self, "_warned_temperature", False):
            logger.warning(
                "GeminiCLIAdapter.chat: temperature=%s is silently ignored — "
                "gemini -p CLI does not expose --temperature today. "
                "(once-per-instance warning)",
                temperature,
            )
            self._warned_temperature = True
        if max_tokens is not None and not getattr(self, "_warned_max_tokens", False):
            logger.warning(
                "GeminiCLIAdapter.chat: max_tokens=%s is silently ignored — "
                "gemini -p CLI does not expose --max-tokens today. "
                "(once-per-instance warning)",
                max_tokens,
            )
            self._warned_max_tokens = True
        if json_schema is not None and not getattr(self, "_warned_schema", False):
            logger.warning(
                "GeminiCLIAdapter.chat: json_schema is not enforced — gemini "
                "-p CLI has no --json-schema flag. Use --provider gemini "
                "(metered SDK) for native structured output. "
                "(once-per-instance warning)"
            )
            self._warned_schema = True

        prompt_text = _build_prompt(messages, system_prompt)

        argv = [self._gemini_bin, "-p", prompt_text, "-o", "json"]
        # Empty model => let the CLI use its subscription default. Only
        # pass -m when a concrete model is requested.
        if model:
            argv.extend(["-m", model])

        # Scrubbed env keeps GEMINI_API_KEY / GOOGLE_API_KEY (and the
        # *_API_KEY suffix forward-compat denylist) out of the subprocess
        # so the CLI bills the subscription, not the metered API.
        env = scrub_env_for_subprocess()

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
                f"GeminiCLIAdapter.chat timed out after {self._timeout_seconds}s "
                f"on model {model or '<subscription-default>'!r}"
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterError(
                f"GeminiCLIAdapter: gemini binary not found at "
                f"{self._gemini_bin!r}. Set GEMINI_BIN env or install via "
                "`npm i -g @google/gemini-cli`"
            ) from exc
        duration_s = time.monotonic() - start

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "")[-500:]
            raise AdapterError(
                f"GeminiCLIAdapter.chat exit={proc.returncode} on model "
                f"{model or '<subscription-default>'!r}: stderr={stderr_tail!r}"
            )

        payload = _parse_gemini_json(proc.stdout or "")

        text = payload.get("response", "") or ""
        stats = payload.get("stats") or {}
        input_tokens, output_tokens, cached = _aggregate_tokens(stats)

        # Pick the primary model name for pricing: the model the caller
        # asked for, else the first model in stats, else _default.
        priced_model = model or next(
            iter((stats.get("models") or {}).keys()), ""
        )
        in_per_mtok, out_per_mtok = PRICING.get(priced_model, PRICING["_default"])
        api_metered_cost_usd = (
            (input_tokens / 1_000_000.0) * in_per_mtok
            + (output_tokens / 1_000_000.0) * out_per_mtok
        )

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=0.0,  # subscription path — flat-rate, not per-call
            duration_s=duration_s,
            cached=cached > 0,
            raw_response={
                "billing_path": "subscription",
                "model": priced_model,
                "session_id": payload.get("session_id"),
                "cached_tokens": cached,
                "api_metered_cost_usd": api_metered_cost_usd,
                "models_invoked": list((stats.get("models") or {}).keys()),
            },
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Streaming via ``gemini -p -o stream-json`` is a future stretch;
        use chat() for now (mirrors ClaudeAdapter.stream)."""
        raise NotImplementedError(
            "GeminiCLIAdapter.stream is a stretch; use chat() for now."
        )
        yield ""  # pragma: no cover — keeps AsyncIterator shape

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return metered-equivalent (input_per_mtok, output_per_mtok).

        Subscription path bills flat-rate; this returns what the call
        WOULD have cost on the metered API (for subscription-savings
        auditing).
        """
        return PRICING.get(model, PRICING["_default"])

    def list_models(self, *, ttl_seconds: int = 86400):
        """Catalog query — delegates to discovery with provider="gemini"
        (same Google catalog as the metered adapter)."""
        from swarph_mesh.discovery import list_models as _list

        return _list(provider="gemini", ttl_seconds=ttl_seconds)
