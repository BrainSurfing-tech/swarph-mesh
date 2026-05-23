"""Antigravity adapter — firejail-sandboxed ``agy -p`` subscription path.

Google discontinues the ``gemini`` CLI in June 2026, replaced by the antigravity
CLI (``agy``). This adapter is the successor to :class:`GeminiCLIAdapter` for the
subscription Gemini worker lane (gemini-3.5-flash). Dual-runs with ``gemini-cli``
until the EOL, then ``gemini-cli`` retires.

**CRITICAL — agy is AGENTIC and uncontained by its own flags.** Empirically
(lab spike 2026-05-23): ``agy -p`` auto-fires fs/bash tools with no permission
gate, has no ``--no-tools`` flag, and ``--sandbox`` does NOT contain the
filesystem. For workers taking arbitrary/untrusted prompts that is a
remote-prompt → host-RCE/exfil risk. So **every invocation runs inside a firejail
OS sandbox** that whitelists only agy's binary + its own runtime dir and drops
everything else. Verified: a read outside the whitelist is BLOCKED (a decoy file
in $HOME was unreachable), while the LLM call still succeeds.

Sandbox profile (AI²-reviewed, droplet DMs #1388/#1389):
  firejail --quiet
    --whitelist=$HOME/.gemini/antigravity-cli   # agy auth + runtime dir
    --whitelist=$HOME/.local/bin/agy            # the binary (self-contained Go)
    --private-tmp --caps.drop=all --nonewprivs --noroot --seccomp
    agy -p <prompt>
Residual (accepted v1): network egress stays open — the LLM call needs outbound
443, so a malicious tool *could* POST to an arbitrary host. fs is sealed (the
bigger hole); net-exfil needs the model to actively target a known endpoint.
Documented residual; revisit if an attack pattern surfaces.

Auth: agy reuses ``~/.gemini/antigravity-cli/antigravity-oauth-token`` (the
antigravity OAuth login) = subscription, ``cost_usd=0.0``. ``scrub_env`` strips
API keys AND ``GOOGLE_APPLICATION_CREDENTIALS`` + project vars (the gap
``GeminiCLIAdapter``'s scrub missed) so no metered-billing fallback can fire.

Output is plain text (agy has no JSON/stats mode) → token counts unavailable
(``input_tokens``/``output_tokens`` = 0). Per-call audit metadata is appended to
an audit log for the rollout-observation window (the firejail ``--trace`` audit
is incompatible with ``--seccomp``, so observation happens at this adapter layer).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Optional

from swarph_shared import scrub_env_for_subprocess

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


logger = logging.getLogger(__name__)

# antigravity has no -m flag; the model is fixed to its default (gemini-3.5-flash
# as of 2026-05). Recorded for attribution only — not selectable per-call.
DEFAULT_MODEL = "gemini-3.5-flash"
# firejail + agy cold-start is heavier than bare gemini -p; give generous headroom.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ANTIGRAVITY_TIMEOUT", "180"))
# scrub_env strips *_API_KEY by suffix; these billing-relevant vars do NOT end in
# _API_KEY and must be stripped explicitly so agy can't fall back to metered
# Vertex/GCP billing (the GeminiCLIAdapter scrub-gap, droplet DM #1386/#1389).
_EXTRA_SCRUB = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_PROJECT",
    "VERTEX_LOCATION",
)
_AUDIT_LOG = os.environ.get(
    "ANTIGRAVITY_AUDIT_LOG",
    str(Path.home() / ".gemini" / "antigravity-cli" / "swarph_audit.jsonl"),
)


def _resolve_agy_bin() -> str:
    if os.environ.get("AGY_BIN"):
        return os.environ["AGY_BIN"]
    home_local = Path.home() / ".local" / "bin" / "agy"
    if home_local.exists():
        return str(home_local)
    import shutil

    return shutil.which("agy") or str(home_local)


def _resolve_firejail_bin() -> str:
    if os.environ.get("FIREJAIL_BIN"):
        return os.environ["FIREJAIL_BIN"]
    import shutil

    return shutil.which("firejail") or "/usr/bin/firejail"


def _build_prompt(messages: list[ChatMessage], system_prompt: Optional[str]) -> str:
    """Render messages into one prompt string. Same shape as the other CLI
    adapters: ``[ROLE]\\ncontent`` blocks separated by blank lines."""
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt.strip())
    for m in messages:
        parts.append(f"[{m.role.upper()}]\n{m.content}")
    return "\n\n".join(parts)


def _scrubbed_env() -> dict:
    """scrub_env_for_subprocess + the explicit extra billing vars agy could
    use for a metered fallback. Forces the subscription OAuth path."""
    env = scrub_env_for_subprocess()
    for k in _EXTRA_SCRUB:
        env.pop(k, None)
    return env


def _firejail_argv(firejail_bin: str, agy_bin: str, prompt: str) -> list[str]:
    home = str(Path.home())
    return [
        firejail_bin, "--quiet",
        f"--whitelist={home}/.gemini/antigravity-cli",
        f"--whitelist={agy_bin}",
        "--private-tmp", "--caps.drop=all", "--nonewprivs", "--noroot", "--seccomp",
        agy_bin, "-p", prompt,
    ]


def _audit(record: dict) -> None:
    """Append one JSON line of call metadata. Best-effort — never blocks the
    call. Rollout-observation window substitute for firejail --trace (which is
    seccomp-incompatible). A silently-blocked legit path surfaces here as a
    nonzero exit / timeout / odd-short response."""
    try:
        Path(_AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


class AntigravityAdapter:
    """``LLMAdapter`` for subscription-billed Gemini via firejail-sandboxed
    ``agy -p`` — the gemini-cli successor (June 2026 EOL migration)."""

    name = "antigravity"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,  # ignored — subscription via agy OAuth
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        agy_bin: Optional[str] = None,
        firejail_bin: Optional[str] = None,
    ):
        if api_key is not None:
            logger.warning(
                "AntigravityAdapter ignores api_key — subscription path uses "
                "agy's OAuth (~/.gemini/antigravity-cli). kwarg is a no-op."
            )
        self._timeout_seconds = timeout_seconds
        self._agy_bin = agy_bin or _resolve_agy_bin()
        self._firejail_bin = firejail_bin or _resolve_firejail_bin()

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Invoke firejail-sandboxed ``agy -p``. ``model``/``temperature``/
        ``max_tokens``/``json_schema`` are accepted for Protocol shape but agy
        exposes none of them (fixed model, no flags); a once-per-instance
        warning fires if a non-default is passed."""
        if model and model != DEFAULT_MODEL and not getattr(self, "_warned_model", False):
            logger.warning(
                "AntigravityAdapter: agy has no -m flag; model=%s ignored "
                "(fixed to %s). (once-per-instance)", model, DEFAULT_MODEL,
            )
            self._warned_model = True

        prompt_text = _build_prompt(messages, system_prompt)
        argv = _firejail_argv(self._firejail_bin, self._agy_bin, prompt_text)
        env = _scrubbed_env()

        start = time.monotonic()
        timed_out = False
        try:
            proc = await asyncio.to_thread(
                subprocess.run, argv, env=env, capture_output=True,
                text=True, timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            # generous stderr budget on the failure path (droplet DM #1391):
            # firejail denial patterns ("Reading blacklist"/"Permission denied"/
            # "Operation not permitted") may sit far from the tail.
            to_err = (exc.stderr or b"")
            to_err = to_err.decode(errors="replace") if isinstance(to_err, bytes) else (to_err or "")
            _audit({"ts": time.time(), "prompt_sha8": hashlib.sha256(prompt_text.encode()).hexdigest()[:8],
                    "timed_out": True, "duration_s": round(time.monotonic() - start, 2),
                    "stderr_tail": to_err[-2048:]})
            raise AdapterError(
                f"AntigravityAdapter timed out after {self._timeout_seconds}s "
                "(firejail+agy). Check agy auth + firejail sandbox."
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterError(
                f"AntigravityAdapter: firejail or agy not found "
                f"(firejail={self._firejail_bin!r}, agy={self._agy_bin!r}). "
                "Install firejail + antigravity CLI."
            ) from exc
        duration_s = time.monotonic() - start

        text = (proc.stdout or "").strip()
        # Failure path gets a generous 2KB stderr budget (firejail denial
        # patterns may sit far from the tail); success path skips stderr.
        stderr_tail = (proc.stderr or "")[-2048:] if proc.returncode != 0 else ""
        _audit({
            "ts": time.time(),
            "prompt_sha8": hashlib.sha256(prompt_text.encode()).hexdigest()[:8],
            "exit": proc.returncode, "duration_s": round(duration_s, 2),
            "resp_len": len(text), "timed_out": timed_out,
            "stderr_tail": stderr_tail,
        })

        if proc.returncode != 0:
            raise AdapterError(
                f"AntigravityAdapter exit={proc.returncode}: stderr={stderr_tail!r}"
            )
        if not text:
            raise AdapterError("AntigravityAdapter: empty response from agy -p")

        return LLMResponse(
            text=text,
            input_tokens=0,   # agy has no stats output
            output_tokens=0,
            cost_usd=0.0,     # subscription — flat-rate
            duration_s=duration_s,
            cached=False,
            raw_response={
                "billing_path": "subscription",
                "model": DEFAULT_MODEL,
                "sandbox": "firejail",
                "token_stats": "unavailable (agy text-only output)",
                "net_egress_residual": "open (LLM call needs 443; documented v1)",
            },
        )

    async def stream(self, messages, model, **kwargs) -> AsyncIterator[str]:
        raise NotImplementedError("AntigravityAdapter.stream not supported; use chat().")
        yield ""  # pragma: no cover

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Subscription path — flat-rate. Returns (0.0, 0.0); no metered cost."""
        return (0.0, 0.0)

    def list_models(self, *, ttl_seconds: int = 86400):
        from swarph_mesh.discovery import list_models as _list

        return _list(provider="gemini", ttl_seconds=ttl_seconds)
