"""Mistral-Vibe CLI adapter — firejail-sandboxed ``vibe -p`` subscription path.

The **subscription** Mistral lane, sibling of :class:`GrokCLIAdapter`
(``grok_cli.py``) and :class:`AntigravityAdapter`: Mistral's ``vibe`` CLI runs
against a **Mistral subscription**, so ``cost_usd=0.0`` at the mesh layer.

>>> READ THIS BEFORE "HARDENING" THE CREDENTIAL HANDLING, BECAUSE THE SIBLING
    LANE TEACHES THE OPPOSITE LESSON. <<< Mistral issues **subscription API
keys**: ``MISTRAL_API_KEY`` IS the subscription path, not a metered one. That
inverts the grok lane's design intent — ``grok_cli.py`` deliberately scrubs
``XAI_API_KEY`` so grok can ONLY use its $0 OIDC session token and no
pay-per-token fallback can fire. Here there is no metered sibling to fall back
to, and **scrubbing the key does not harden this lane, it breaks it**. A
maintainer arriving from ``grok_cli.py`` and applying its rule by analogy would
produce a worker with no credential — the exact born-broken failure the
allowlist below exists to prevent. The presence of an API key is NOT evidence of
metered billing; on this vendor it is evidence of the subscription.

**JURISDICTION — the reason this lane exists beyond capacity.** Mistral AI is a
French (EU-domiciled) provider, so this is the mesh's only lane whose vendor is
inside the EU. ``raw_response["jurisdiction_declared"] = "eu-unattested"`` is
carried so a router or an audit can SELECT on it — note the value, never a bare
``"eu"``; see below for why the qualification lives IN it.

    >>> DECLARED, NOT ATTESTED — AND THE VALUE ITSELF SAYS SO. <<< That the
    vendor is EU-domiciled is a fact about the company. Whether THIS
    subscription's terms guarantee EU-only processing/retention is a contract
    question no code here can verify. So the value is the string
    ``"eu-unattested"``, not ``"eu"``: a router filtering on ``== "eu"`` fails
    CLOSED rather than matching. ``jurisdiction_attested = False`` remains as
    the machine-readable half. A field claiming ``gdpr_compliant`` would assert
    something this process cannot observe.

**CRITICAL — vibe is AGENTIC**, so the same containment posture as the grok-cli
lane applies. Two things make this lane STRICTLY TIGHTER than that one:

  1. ``--disabled-tools '*'`` — vibe takes a native tool filter, so the worker
     runs with NO TOOLS AT ALL. The grok lane could only trim (``--no-subagents``)
     and rely on firejail for the rest; here the agentic surface is closed at the
     application layer as well as the OS layer.

     WHICH HALF OF THAT IS ACTUALLY COVERED, since it is an application-layer
     control in front of an OS-layer seal. A review attack asked what happens if
     a future vibe RENAMES or drops the flag — would the adapter silently run an
     agentic CLI *with* tools? MEASURED, and REFUTED: vibe is argparse-based and
     exits 2 on an unrecognised flag, so a rename breaks this lane LOUDLY rather
     than quietly re-enabling tools. What is NOT covered is a SEMANTIC change
     (flag kept, ``*`` no longer meaning "all") — no test on our side can catch
     that; only a behavioural probe asserting a tool actually refuses to run.
     Note the asymmetry deliberately: the test asserts the flag is PASSED, not
     that it is HONORED. That is safe here only because of THEIR parser, not
     because of our test — so do not "improve" it into a false sense of coverage.
  2. **An EPHEMERAL ``VIBE_HOME`` per call.** vibe keeps sessions/history/trust
     under ``$VIBE_HOME``; pointing it at a fresh 0700 temp dir per invocation
     makes the worker stateless BY CONSTRUCTION. The grok lane had to whitelist
     the operator's ``~/.grok`` and then blacklist its memory subdir — a
     seal-after-the-fact. Nothing here touches ``~/.vibe``, so there is no
     operator state to contaminate and no memory dir to blacklist.

A third difference removes a whole file-exposure class: **the prompt is piped on
STDIN**, never written to disk. ``vibe -p`` with no TEXT reads stdin (verified by
execution). The grok lane needs a ``--prompt-file`` and therefore needs 0700/0600
modes and an unlink in a ``finally`` — none of that exists here because the
prompt never lands in the filesystem.

Sandbox profile::

    firejail --quiet
      --whitelist=<venv_root>       # the uv-tool venv: interpreter + vibe pkg
      --whitelist=<vibe_home>       # ephemeral per-call VIBE_HOME
      --whitelist=<workdir>         # ephemeral empty cwd
      --private-tmp --caps.drop=all --nonewprivs --noroot --seccomp
      vibe -p --output json --trust --disabled-tools '*'
           --max-price N --max-turns N --workdir <workdir>

**The cost rail no other lane has.** ``--max-price DOLLARS`` interrupts the
session if cost exceeds the limit — an enforceable ceiling at the CALL SITE
rather than an after-the-fact bill. It applies ONLY in programmatic (``-p``)
mode, which is exactly this lane. It is passed on every call.

Residual — THE VENV ROOT IS THE TRUST BOUNDARY FOR THE TOOL INSTALL. vibe is a
python console-script, so the firejail whitelist must admit its interpreter and
site-packages; whitelisting only the script would be a false seal. That is not
wider than "the package that implements vibe", but it IS wider than one binary:
whatever else lives inside that uv-tool venv is admitted too. Keep the tool env
DEDICATED — do not share it with other uv tools. (Raised in review.)

Residual (accepted v1, same as the grok/agy lanes): network egress stays open —
the LLM call needs outbound 443. Here the credential is an ENV VAR
(``MISTRAL_API_KEY``) rather than grok's on-disk ``auth.json``, so the exfil
lever is an injected tool reading its own environment. With ``--disabled-tools
'*'`` there is no tool to inject into, but that is an application-layer control;
treat the residual as present. Egress allowlisting would close it and remains a
mesh-wide feature-add.

Token accounting is UNAVAILABLE: ``--output json`` emits message/reasoning
entries with no usage block (verified by execution — the hypothesis that JSON
output would carry token counts was tested and REFUTED). ``input_tokens`` /
``output_tokens`` are therefore 0, exactly as in the grok-cli lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator, Optional

from swarph_shared import scrub_env_for_subprocess

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


logger = logging.getLogger(__name__)

#: Recorded for attribution only. The vibe CLI exposes no per-call model flag;
#: the model is whatever the vibe build defaults to. Because VIBE_HOME is
#: EPHEMERAL, no operator ``config.toml`` is in scope either — so the lane is
#: reproducible across boxes regardless of operator config drift.
DEFAULT_MODEL = "mistral-vibe"

DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("VIBE_CLI_TIMEOUT", "180"))

#: Default hard cost ceiling per call, in dollars (``--max-price``). Override
#: per-instance or via env. This is a CEILING, not a budget: vibe interrupts the
#: session when it is exceeded.
DEFAULT_MAX_PRICE = float(os.environ.get("VIBE_CLI_MAX_PRICE", "0.25"))

#: Pure-inference worker: one assistant turn, no tool loop.
DEFAULT_MAX_TURNS = int(os.environ.get("VIBE_CLI_MAX_TURNS", "1"))

#: >>> THE ONE VAR THIS LANE MUST RECEIVE FROM THE PARENT. <<<
#: ``scrub_env_for_subprocess`` strips every ``*_API_KEY`` by suffix — which
#: includes MISTRAL_API_KEY, the credential this lane authenticates with. The
#: grok lane's allowlist is EMPTY because its auth is a FILE (``auth.json``);
#: copying that shape here yields a worker with no credential that fails at the
#: provider with "Missing MISTRAL_API_KEY", i.e. a missing credential wearing the
#: costume of a broken adapter. ALLOWLIST — never a denylist of the others.
_ENV_ALLOWLIST: frozenset = frozenset({"MISTRAL_API_KEY"})

#: >>> ``VIBE_*`` IS SCRUBBED BY PREFIX, NEVER ENUMERATED. <<< The vendor's own
#: ``--help`` declares the namespace OPEN::
#:
#:     VIBE_*          Override any config field (e.g. VIBE_ACTIVE_MODEL=local).
#:
#: An enumeration of an open namespace FAILS OPEN. The first cut of this adapter
#: listed five vars and let ``VIBE_ACTIVE_MODEL`` (the vendor's OWN example,
#: which redirects the model) and any future field straight through into the
#: jail — the same allowlist-never-denylist rule this file already applies to the
#: credential, applied unevenly four lines below it. Caught in review.
#: Derive containment from the vendor's DECLARED domain, not from the vars we
#: happened to think of.
_SCRUB_PREFIXES = ("VIBE_",)

#: The ``MISTRAL_*`` redirect class, which the suffix scrub misses because these
#: do NOT end in ``_API_KEY``. Enumerating here is defensible: unlike ``VIBE_*``,
#: that namespace is not declared open by the vendor. Same defect class found in
#: review on the grok lane (PR #31 MEDIUM).
_EXTRA_SCRUB = (
    "MISTRAL_API_BASE",
    "MISTRAL_BASE_URL",
    "MISTRAL_API_HOST",
)

_AUDIT_LOG = os.environ.get(
    "VIBE_CLI_AUDIT_LOG",
    str(Path.home() / ".swarph" / "vibe_cli_audit.jsonl"),
)


def _resolve_vibe_bin() -> str:
    """Absolute vibe binary. Prefer an env override, then the uv-tool install,
    then ``~/.local/bin/vibe``, then PATH.

    NEVER trust PATH order alone — the grok lane was shadowed by a stale npm
    install at ``/usr/local/bin/grok``; ``vibe`` is a common enough word that the
    same collision is plausible.
    """
    override = os.environ.get("VIBE_BIN")
    if override:
        if not os.path.isabs(override):
            raise AdapterError(
                f"VIBE_BIN must be an absolute path (got {override!r}); a relative "
                "value re-opens PATH-shadowing by an unrelated 'vibe' on PATH."
            )
        return override
    uv_tool = Path.home() / ".local" / "share" / "uv" / "tools" / "mistral-vibe" / "bin" / "vibe"
    if uv_tool.exists():
        return str(uv_tool)
    home_local = Path.home() / ".local" / "bin" / "vibe"
    if home_local.exists():
        # Resolve the shim to its real venv target so the firejail whitelist can
        # be derived from the interpreter's own tree.
        return str(home_local.resolve())
    return shutil.which("vibe") or str(home_local)


def _resolve_firejail_bin() -> str:
    if os.environ.get("FIREJAIL_BIN"):
        return os.environ["FIREJAIL_BIN"]
    return shutil.which("firejail") or "/usr/bin/firejail"


def _venv_root(vibe_bin: str) -> str:
    """The venv containing the interpreter that runs vibe.

    vibe is a PYTHON console-script, not a static binary: whitelisting only the
    script would jail away its interpreter and site-packages. The venv root is
    ``<bin>/..`` — whitelisting it admits interpreter + package tree together.
    """
    return str(Path(vibe_bin).resolve().parent.parent)


def _build_prompt(messages: list[ChatMessage], system_prompt: Optional[str]) -> str:
    """Render messages into one prompt string. Same shape as the sibling CLI
    adapters: ``[ROLE]\\ncontent`` blocks separated by blank lines."""
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt.strip())
    for m in messages:
        parts.append(f"[{m.role.upper()}]\n{m.content}")
    return "\n\n".join(parts)


def _scrubbed_env() -> dict:
    """Canonical scrub, then RE-ADD the allowlisted credential.

    Order matters and is deliberate: scrub first (so every unrelated provider
    key, and the redirect class, is gone), then put back exactly the one var in
    ``_ENV_ALLOWLIST`` from the parent environment. Building the env by
    subtraction instead would leave any newly-introduced Mistral var to pass
    through by default.
    """
    env = scrub_env_for_subprocess()
    # Prefix scrub FIRST — the open namespace, by construction rather than by
    # enumeration. VIBE_HOME is caught here too and re-set explicitly per call.
    for k in [k for k in env if k.startswith(_SCRUB_PREFIXES)]:
        env.pop(k, None)
    for k in _EXTRA_SCRUB:
        env.pop(k, None)
    for k in _ENV_ALLOWLIST:
        val = os.environ.get(k)
        if val:
            env[k] = val
    return env


def _firejail_argv(
    firejail_bin: str,
    vibe_bin: str,
    *,
    vibe_home: str,
    workdir: str,
    max_price: float,
    max_turns: int,
) -> list[str]:
    """Sandbox argv. Whitelist ONLY the venv, the ephemeral home, the ephemeral
    cwd. Notably absent: any path under the operator's ``~/.vibe``."""
    return [
        firejail_bin, "--quiet",
        f"--whitelist={_venv_root(vibe_bin)}",
        f"--whitelist={vibe_home}",
        f"--whitelist={workdir}",
        "--private-tmp", "--caps.drop=all", "--nonewprivs", "--noroot", "--seccomp",
        vibe_bin,
        # -p with NO positional TEXT => read the prompt from stdin, so the prompt
        # never touches the filesystem.
        "-p",
        "--output", "json",
        # Non-interactive: skip the trust prompt for this invocation only (not
        # persisted to trusted_folders.toml — and the ephemeral home is discarded
        # anyway).
        "--trust",
        # No tools at all. Application-layer closure of the agentic surface, on
        # top of the OS-layer firejail seal.
        "--disabled-tools", "*",
        "--max-price", str(max_price),
        "--max-turns", str(max_turns),
        "--workdir", workdir,
    ]


def _audit(record: dict) -> None:
    """Append one JSON line of call metadata. Best-effort — never blocks the
    call. Rollout-observation substitute for firejail ``--trace`` (seccomp-
    incompatible), same as the grok/agy lanes."""
    try:
        Path(_AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _extract_text(stdout: str) -> str:
    """Pull the assistant's reply out of ``--output json``.

    The payload is a LIST of entries: ``user`` messages, ``reasoning`` entries
    (the model's chain of thought) and ``assistant`` messages. Take the text of
    the assistant messages ONLY.

    >>> ``reasoning`` MUST NOT be returned. <<< It is the model's private
    deliberation, and a mesh consumer that received it would treat it as the
    answer. Selecting by ``type == "message" and role == "assistant"`` is an
    ALLOWLIST — a "skip reasoning" denylist would silently admit whatever entry
    type a future vibe release introduces.
    """
    try:
        entries = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise AdapterError(
            f"VibeCLIAdapter: --output json did not parse ({exc}); "
            f"first 200 chars: {stdout[:200]!r}"
        ) from exc
    if not isinstance(entries, list):
        raise AdapterError(
            f"VibeCLIAdapter: expected a JSON list from --output json, got "
            f"{type(entries).__name__}"
        )
    parts: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "message" or e.get("role") != "assistant":
            continue
        for block in e.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p).strip()


class VibeCLIAdapter:
    """``LLMAdapter`` for subscription-billed Mistral via firejail-sandboxed
    ``vibe -p`` — the mesh's EU-domiciled provider lane."""

    name = "vibe-cli"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        vibe_bin: Optional[str] = None,
        firejail_bin: Optional[str] = None,
        max_price: float = DEFAULT_MAX_PRICE,
        max_turns: int = DEFAULT_MAX_TURNS,
    ):
        # Unlike the grok lane, api_key is MEANINGFUL here: vibe authenticates
        # with MISTRAL_API_KEY. An explicitly-passed key is used for this
        # instance without touching the process environment.
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._vibe_bin = vibe_bin or _resolve_vibe_bin()
        self._firejail_bin = firejail_bin or _resolve_firejail_bin()
        self._max_price = max_price
        self._max_turns = max_turns

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Invoke firejail-sandboxed ``vibe -p``, prompt piped on stdin.

        ``model`` / ``temperature`` / ``json_schema`` are accepted for Protocol
        shape; the vibe CLI exposes none of them. ``max_tokens`` DOES map — to
        vibe's ``--max-tokens`` session ceiling — but its semantics differ
        (prompt+completion across the session, not completion-only), so it is
        deliberately NOT wired: silently reinterpreting a caller's parameter is
        worse than not honouring it.
        """
        if model and model != DEFAULT_MODEL and not getattr(self, "_warned_model", False):
            logger.warning(
                "VibeCLIAdapter: the vibe CLI has no per-call model flag; "
                "model=%s ignored (attribution: %s). (once-per-instance)",
                model, DEFAULT_MODEL,
            )
            self._warned_model = True

        # >>> THE CANNOT-PROCEED BRANCH, CHECKED BEFORE ANY WORK. <<< Without the
        # credential vibe fails inside the sandbox with a provider-side message,
        # which reads as "the adapter is broken" rather than "the box is not
        # configured". Name the cause and the fix here instead.
        if not self._api_key and not os.environ.get("MISTRAL_API_KEY"):
            raise AdapterError(
                "VibeCLIAdapter: no MISTRAL_API_KEY. The mesh env scrub strips "
                "every *_API_KEY by suffix, so it must be present in THIS "
                "process's environment (or passed as api_key=). Set it in the "
                "service unit, or export it from ~/.vibe/.env; `vibe --setup` "
                "writes that file. This is a missing credential, NOT a broken lane."
            )

        prompt_text = _build_prompt(messages, system_prompt)
        prompt_sha8 = hashlib.sha256(prompt_text.encode()).hexdigest()[:8]

        env = _scrubbed_env()
        if self._api_key:
            env["MISTRAL_API_KEY"] = self._api_key

        start = time.monotonic()
        # Ephemeral VIBE_HOME + cwd: statelessness by construction. mkdtemp is
        # 0700 by default, and both trees are removed in the finally.
        vibe_home = tempfile.mkdtemp(prefix="swarph-vibe-home-")
        workdir = tempfile.mkdtemp(prefix="swarph-vibe-work-")
        env["VIBE_HOME"] = vibe_home
        try:
            argv = _firejail_argv(
                self._firejail_bin, self._vibe_bin,
                vibe_home=vibe_home, workdir=workdir,
                max_price=self._max_price, max_turns=self._max_turns,
            )
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, argv, env=env, input=prompt_text,
                    capture_output=True, text=True, timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                to_err = exc.stderr or ""
                to_err = to_err.decode(errors="replace") if isinstance(to_err, bytes) else to_err
                _audit({"ts": time.time(), "prompt_sha8": prompt_sha8,
                        "timed_out": True, "duration_s": round(time.monotonic() - start, 2),
                        "stderr_tail": to_err[-512:]})
                raise AdapterError(
                    f"VibeCLIAdapter timed out after {self._timeout_seconds}s "
                    "(firejail+vibe). Check MISTRAL_API_KEY and firejail."
                ) from exc
            except FileNotFoundError as exc:
                raise AdapterError(
                    f"VibeCLIAdapter: firejail or vibe not found "
                    f"(firejail={self._firejail_bin!r}, vibe={self._vibe_bin!r}). "
                    "Install firejail + the Mistral Vibe CLI."
                ) from exc
        finally:
            shutil.rmtree(vibe_home, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)
        duration_s = time.monotonic() - start

        # Bound the stderr tail: it can carry response fragments and lands in the
        # PERSISTENT audit log AND the raised AdapterError.
        stderr_tail = (proc.stderr or "")[-512:] if proc.returncode != 0 else ""
        text = _extract_text(proc.stdout or "") if proc.returncode == 0 else ""
        _audit({
            "ts": time.time(), "prompt_sha8": prompt_sha8,
            "exit": proc.returncode, "duration_s": round(duration_s, 2),
            "resp_len": len(text), "timed_out": False, "stderr_tail": stderr_tail,
            "max_price": self._max_price,
        })

        if proc.returncode != 0:
            raise AdapterError(
                f"VibeCLIAdapter exit={proc.returncode}: stderr={stderr_tail!r}"
            )
        if not text:
            raise AdapterError(
                "VibeCLIAdapter: no assistant message in the JSON output. (If the "
                "run hit --max-price, vibe interrupts the session and the "
                "assistant turn may be absent — the ceiling is working, not failing.)"
            )

        return LLMResponse(
            text=text,
            input_tokens=0,   # vibe --output json carries no usage block
            output_tokens=0,
            cost_usd=0.0,     # subscription — flat-rate at the mesh layer
            duration_s=duration_s,
            cached=False,
            raw_response={
                "billing_path": "subscription",
                "model": DEFAULT_MODEL,
                "sandbox": "firejail",
                "tools": "none (--disabled-tools '*')",
                "state": "ephemeral VIBE_HOME (stateless by construction)",
                "max_price_usd": self._max_price,
                # >>> THE QUALIFICATION IS IN THE VALUE, NOT BESIDE IT. <<< The
                # value is "eu-unattested", never "eu", so a naive router doing
                # `if raw["jurisdiction_declared"] == "eu"` FAILS CLOSED — it
                # does not match — instead of matching and silently routing
                # personal data on an unverified contract claim. A consumer that
                # genuinely wants unattested-EU must ask for it BY NAME.
                # A value that is only safe when read together with a separate
                # qualifying flag is the shape that produced a live money-path
                # defect in this mesh (a caller read the value, dropped the
                # `complete` flag, and reported sale proceeds as profit). A
                # docstring forbidding the misread is not a mechanism; making the
                # wrong reading INEXPRESSIBLE is. Caught in review.
                "jurisdiction_declared": "eu-unattested",
                "jurisdiction_basis": "vendor domicile (Mistral AI SAS, France)",
                "jurisdiction_attested": False,
                "token_stats": "unavailable (vibe JSON output has no usage block)",
                "net_egress_residual": "open (LLM call needs 443; documented v1)",
            },
        )

    async def stream(self, messages, model, **kwargs) -> AsyncIterator[str]:
        raise NotImplementedError("VibeCLIAdapter.stream not supported; use chat().")
        yield ""  # pragma: no cover

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Subscription path — flat-rate. Returns (0.0, 0.0); no metered cost.

        >>> ``cost_usd = 0.0`` IS A BILLING PATH, NOT A MEASURED SPEND. <<< A
        consumer doing cost accounting must not read it as "this call was free to
        produce" — the subscription is paid, and the CLI is tracking a real cost
        internally (that is what ``--max-price`` interrupts on). We simply cannot
        observe the number. The enforceable figure is the CEILING, exposed as
        ``raw_response["max_price_usd"]``. Raised in review, and it is the same
        distinction as the token counts: report what is known, never infer.
        """
        return (0.0, 0.0)
