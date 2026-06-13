"""Grok-CLI adapter — firejail-sandboxed ``grok --prompt-file`` subscription path.

This is the **subscription** Grok lane, the sibling of :class:`GrokAdapter`
(``grok.py``, the *metered* ``api.x.ai/v1`` path) exactly as
:class:`AntigravityAdapter` is the subscription sibling of the metered Gemini
adapters. xAI's official **Grok Build CLI** (``grok``) authenticates via an
OIDC *session token* (``~/.grok/auth.json``, ``auth_mode: oidc``) that rides the
**SuperGrok Heavy** subscription — so ``cost_usd=0.0``, no metered
``XAI_API_KEY`` involved. Model is ``grok-build`` (xAI's coding model, 512K ctx).

**CRITICAL — grok is AGENTIC.** The Grok Build CLI auto-uses fs/bash tools and
MCP servers; for a worker taking arbitrary/untrusted prompts that is a
remote-prompt → host-RCE/exfil risk. So **every invocation runs inside a firejail
OS sandbox** identical in shape to the agy/Antigravity lane: whitelist only
grok's binary + its ``~/.grok`` runtime dir (auth + sessions) + the prompt-file
dir, drop caps, ``noroot``/``nonewprivs``/``seccomp``/``private-tmp``.

Sandbox profile:
  firejail --quiet
    --whitelist=$HOME/.grok               # grok auth + sessions + native binary
    --whitelist=<grok_bin>                # the GROK_BIN symlink (absolute)
    --whitelist=$HOME/.local/bin          # symlink chain target
    --whitelist=<prompt_dir>              # the temp --prompt-file lives here
    --private-tmp --caps.drop=all --nonewprivs --noroot --seccomp
    grok --prompt-file <file> --output-format plain --no-memory --no-subagents

**Stateless by construction.** grok supports global cross-session memory
(``~/.grok/config.toml`` ``[memory] enabled=true``). A mesh worker must be
stateless — each call independent (no verdict contamination), and untrusted
prompts must neither read from nor write to any operator's personal grok memory.
So every call passes ``--no-memory``. ``--no-subagents`` likewise: a single mesh
call needs no fan-out, and it trims the agentic surface (a
``permission_mode=always-approve`` config makes grok auto-fire tools; firejail
seals the fs, this caps the rest).

Residual (accepted v1, same as Antigravity): network egress stays open — the LLM
call needs outbound 443, so a malicious tool *could* POST to an arbitrary host.
fs is sealed (the bigger hole). Documented residual; revisit if an attack
pattern surfaces.

Prompt delivery: grok's NATIVE ``--prompt-file`` (single-turn: prints the
response to stdout and exits) — no argv length cap (unlike agy's ~4128). The
prompt is written to a uuid-named file under ``prompt_dir`` (default
``~/.grok/swarph-prompts``) and always cleaned up. Call the **absolute**
``GROK_BIN`` (``~/.local/bin/grok``): a stale ``@vibe-kit/grok-cli`` npm install
once collided at ``/usr/local/bin/grok``, so PATH order must not decide.

Output is plain text → token counts unavailable (``input_tokens`` /
``output_tokens`` = 0). Per-call audit metadata is appended to an audit log for
the rollout-observation window (firejail ``--trace`` is seccomp-incompatible, so
observation happens at this adapter layer — same as Antigravity).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

from swarph_shared import scrub_env_for_subprocess

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


logger = logging.getLogger(__name__)

# The Grok Build CLI single-turn path is pinned to grok-build by the
# subscription; recorded for attribution only — not selectable per-call here.
DEFAULT_MODEL = "grok-build"
# firejail + grok cold-start (Rust binary + WebSocket relay handshake) is
# heavier than a bare API call; generous headroom.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("GROK_CLI_TIMEOUT", "180"))
# scrub_env strips *_API_KEY by suffix, which already covers XAI_API_KEY and
# GROK_API_KEY — but GROK_CODE_XAI_API_KEY also ends in _API_KEY so it's covered
# too. We list them explicitly anyway as a belt-and-suspenders guard + as
# documentation of exactly which metered keys must never reach grok (so it can
# ONLY use the $0 OIDC session token, never pay-per-token).
_EXTRA_SCRUB = (
    "XAI_API_KEY",
    "GROK_API_KEY",
    "GROK_CODE_XAI_API_KEY",
)
_DEFAULT_PROMPT_DIR = str(Path.home() / ".grok" / "swarph-prompts")
_AUDIT_LOG = os.environ.get(
    "GROK_CLI_AUDIT_LOG",
    str(Path.home() / ".grok" / "swarph_audit.jsonl"),
)


def _resolve_grok_bin() -> str:
    """Absolute GROK_BIN. Prefer the env override, then the official native
    install at ~/.local/bin/grok, then PATH. NEVER trust PATH order alone — a
    stale @vibe-kit npm install previously shadowed it at /usr/local/bin/grok."""
    if os.environ.get("GROK_BIN"):
        return os.environ["GROK_BIN"]
    home_local = Path.home() / ".local" / "bin" / "grok"
    if home_local.exists():
        return str(home_local)
    import shutil

    return shutil.which("grok") or str(home_local)


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
    """scrub_env_for_subprocess + the explicit metered xAI keys. Forces the
    subscription OIDC-session path; no pay-per-token fallback can fire."""
    env = scrub_env_for_subprocess()
    for k in _EXTRA_SCRUB:
        env.pop(k, None)
    return env


def _firejail_argv(firejail_bin: str, grok_bin: str, prompt_file: str) -> list[str]:
    """Sandbox argv. Whitelist grok's runtime dir (auth + sessions), the binary,
    the ~/.local/bin symlink-chain target, and the prompt-file's own dir."""
    home = str(Path.home())
    prompt_dir = str(Path(prompt_file).parent)
    return [
        firejail_bin, "--quiet",
        f"--whitelist={home}/.grok",
        f"--whitelist={grok_bin}",
        f"--whitelist={home}/.local/bin",
        f"--whitelist={prompt_dir}",
        "--private-tmp", "--caps.drop=all", "--nonewprivs", "--noroot", "--seccomp",
        grok_bin, "--prompt-file", prompt_file, "--output-format", "plain",
        # --no-memory: grok supports global cross-session memory
        # (~/.grok/config.toml [memory]). A mesh worker MUST be stateless — each
        # call independent, and untrusted prompts must neither read from nor
        # write to any operator's personal grok memory. --no-subagents: a single
        # mesh call needs no subagent fan-out, and it caps blast-radius (a
        # permission_mode=always-approve config makes grok auto-fire tools;
        # firejail seals fs, this trims the agentic surface further).
        "--no-memory", "--no-subagents",
    ]


def _audit(record: dict) -> None:
    """Append one JSON line of call metadata. Best-effort — never blocks the
    call. Rollout-observation substitute for firejail --trace (seccomp-
    incompatible). A silently-blocked legit path surfaces here as a nonzero
    exit / timeout / odd-short response."""
    try:
        Path(_AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


class GrokCLIAdapter:
    """``LLMAdapter`` for subscription-billed Grok via firejail-sandboxed
    ``grok --prompt-file`` — xAI's Grok Build CLI on SuperGrok Heavy ($0)."""

    name = "grok-cli"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,  # ignored — subscription via grok OIDC
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        grok_bin: Optional[str] = None,
        firejail_bin: Optional[str] = None,
        prompt_dir: Optional[str] = None,
    ):
        if api_key is not None:
            logger.warning(
                "GrokCLIAdapter ignores api_key — subscription path uses grok's "
                "OIDC session token (~/.grok/auth.json). kwarg is a no-op. "
                "(For the metered api.x.ai path use the 'grok' adapter instead.)"
            )
        self._timeout_seconds = timeout_seconds
        self._grok_bin = grok_bin or _resolve_grok_bin()
        self._firejail_bin = firejail_bin or _resolve_firejail_bin()
        self._prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Invoke firejail-sandboxed ``grok --prompt-file``. ``model`` /
        ``temperature`` / ``max_tokens`` / ``json_schema`` are accepted for
        Protocol shape but the single-turn CLI path exposes none of them (model
        fixed to grok-build by the sub); a once-per-instance warning fires if a
        non-default model is passed."""
        if model and model != DEFAULT_MODEL and not getattr(self, "_warned_model", False):
            logger.warning(
                "GrokCLIAdapter: the single-turn CLI path has no model flag; "
                "model=%s ignored (fixed to %s). (once-per-instance)",
                model, DEFAULT_MODEL,
            )
            self._warned_model = True

        prompt_text = _build_prompt(messages, system_prompt)
        os.makedirs(self._prompt_dir, exist_ok=True)
        prompt_file = os.path.join(self._prompt_dir, f"swarph-{uuid.uuid4().hex}.md")
        prompt_sha8 = hashlib.sha256(prompt_text.encode()).hexdigest()[:8]

        start = time.monotonic()
        try:
            with open(prompt_file, "w", encoding="utf-8") as fh:
                fh.write(prompt_text)
            argv = _firejail_argv(self._firejail_bin, self._grok_bin, prompt_file)
            env = _scrubbed_env()
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, argv, env=env, capture_output=True,
                    text=True, timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                to_err = exc.stderr or ""
                to_err = to_err.decode(errors="replace") if isinstance(to_err, bytes) else to_err
                _audit({"ts": time.time(), "prompt_sha8": prompt_sha8,
                        "timed_out": True, "duration_s": round(time.monotonic() - start, 2),
                        "stderr_tail": to_err[-2048:]})
                raise AdapterError(
                    f"GrokCLIAdapter timed out after {self._timeout_seconds}s "
                    "(firejail+grok). Check grok auth (~/.grok/auth.json) + firejail."
                ) from exc
            except FileNotFoundError as exc:
                raise AdapterError(
                    f"GrokCLIAdapter: firejail or grok not found "
                    f"(firejail={self._firejail_bin!r}, grok={self._grok_bin!r}). "
                    "Install firejail + the xAI Grok Build CLI (`grok login`)."
                ) from exc
        finally:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass
        duration_s = time.monotonic() - start

        text = (proc.stdout or "").strip()
        stderr_tail = (proc.stderr or "")[-2048:] if proc.returncode != 0 else ""
        _audit({
            "ts": time.time(), "prompt_sha8": prompt_sha8,
            "exit": proc.returncode, "duration_s": round(duration_s, 2),
            "resp_len": len(text), "timed_out": False, "stderr_tail": stderr_tail,
        })

        if proc.returncode != 0:
            raise AdapterError(
                f"GrokCLIAdapter exit={proc.returncode}: stderr={stderr_tail!r}"
            )
        if not text:
            raise AdapterError("GrokCLIAdapter: empty response from grok --prompt-file")

        return LLMResponse(
            text=text,
            input_tokens=0,   # grok CLI has no stats output
            output_tokens=0,
            cost_usd=0.0,     # subscription — flat-rate
            duration_s=duration_s,
            cached=False,
            raw_response={
                "billing_path": "subscription",
                "model": DEFAULT_MODEL,
                "sandbox": "firejail",
                "token_stats": "unavailable (grok CLI text-only output)",
                "net_egress_residual": "open (LLM call needs 443; documented v1)",
            },
        )

    async def stream(self, messages, model, **kwargs) -> AsyncIterator[str]:
        raise NotImplementedError("GrokCLIAdapter.stream not supported; use chat().")
        yield ""  # pragma: no cover

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Subscription path — flat-rate. Returns (0.0, 0.0); no metered cost."""
        return (0.0, 0.0)

    def list_models(self, *, ttl_seconds: int = 86400):
        from swarph_mesh.discovery import list_models as _list

        return _list(provider="grok", ttl_seconds=ttl_seconds)
