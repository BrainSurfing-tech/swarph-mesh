"""The two runners: queue jobs (ideation, architect) and local ``claude -p`` (research).

Both call ``guard.check()`` before each job, so every job is gated by the session window,
stop file, job count and wall clock. Neither makes a metered call: the queue lane is the
mesh's subscription workers, and research runs the cell's own logged-in ``claude`` with
the API-key variables scrubbed from the child environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from swarph_shared.subprocess_env import scrub_env_for_subprocess

from swarph_mesh.adapters._resolve import which_or_raise
from swarph_mesh.exceptions import BinaryNotFound

from .ceilings import JobGuard, RunRefused

IDEATION_MODEL = "gemini-4-argon-high"
IDEATION_LANE = "agy"
ARCHITECT_MODEL = "claude-opus-5-5"
ARCHITECT_LANE = "claude"
RESEARCH_MODEL = "claude-sonnet-5-5"
RESEARCH_TIMEOUT = 300
POLL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 600.0

IDEATION_SCHEMA = json.dumps(
    {
        "type": "object",
        "required": [
            "concept_name",
            "proposal_kind",
            "mechanism",
            "key_assumptions",
            "required_research_queries",
        ],
        "properties": {
            "concept_name": {"type": "string"},
            "proposal_kind": {
                "type": "string",
                "enum": ["tooling", "coordination", "ops", "mathematical"],
            },
            "mechanism": {"type": "string"},
            "key_assumptions": {"type": "array", "items": {"type": "string"}},
            "required_research_queries": {"type": "array", "items": {"type": "string"}},
            "estimated_complexity": {"type": "string"},
        },
    }
)
ARCHITECT_SCHEMA = json.dumps(
    {
        "type": "object",
        "required": ["verdict", "mechanism", "audit"],
        "properties": {
            "verdict": {"type": "string"},
            "mechanism": {"type": "string"},
            "audit": {"type": "string"},
        },
    }
)

# Transport: post(url, body) / get(url) -> (http_status, parsed_json). Injected so no
# test touches a network; the CLI passes its own gateway client.
Post = Callable[[str, dict], Tuple[int, dict]]
Get = Callable[[str], Tuple[int, dict]]


def httpx_transport(token: str, timeout: float = 15.0) -> Tuple[Post, Get]:
    """Default transport for library users. The token is only ever put in a header."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"}

    def _parse(r: "httpx.Response") -> Tuple[int, dict]:
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def post(url: str, body: dict) -> Tuple[int, dict]:
        return _parse(httpx.post(url, json=body, headers=headers, timeout=timeout))

    def get(url: str) -> Tuple[int, dict]:
        return _parse(httpx.get(url, headers=headers, timeout=timeout))

    return post, get


class QueueRunner:
    """One ``subscription_chat`` job on POST /council-batch, polled to a terminal state
    before returning, so a run never has two jobs out."""

    def __init__(
        self,
        *,
        stage: str,
        model: str,
        want_lane: str,
        schema: str,
        gateway_url: str,
        cell: str,
        run_id: str,
        guard: JobGuard,
        post: Post,
        get: Get,
        jobs: List[Dict[str, Any]],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        poll_seconds: float = POLL_SECONDS,
        job_timeout: float = JOB_TIMEOUT_SECONDS,
    ):
        self.stage, self.model, self.want_lane, self.schema = (
            stage,
            model,
            want_lane,
            schema,
        )
        self.gateway_url = gateway_url.rstrip("/")
        self.cell, self.run_id, self.guard = cell, run_id, guard
        self.post, self.get, self.jobs = post, get, jobs
        self.sleep, self.clock = sleep, clock
        self.poll_seconds, self.job_timeout = poll_seconds, job_timeout

    def __call__(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self.guard.check()
        system, prompt = _render_prompt(self.stage, ctx)
        debate_id = f"skunkworks-{self.run_id}-{self.stage}-{uuid.uuid4().hex[:8]}"
        body = {
            "debate_id": debate_id,
            "from_node": self.cell,
            "jobs": [
                {
                    "role": "subscription_chat",
                    "model": self.model,
                    "prompt": prompt,
                    "system_prompt": system,
                    "purpose": f"skunkworks-{self.stage}-{self.run_id}"[:128],
                    "response_schema": self.schema,
                }
            ],
        }
        status, resp = self.post(f"{self.gateway_url}/council-batch", body)
        if status != 200:
            raise RunRefused(f"{self.stage}: queue POST refused, HTTP {status}")

        deadline = self.clock() + min(
            self.job_timeout, max(self.guard.remaining_seconds(), 0)
        )
        job: Dict[str, Any] = {}
        while True:
            status, data = self.get(f"{self.gateway_url}/council-batch/{debate_id}")
            if status != 200:
                raise RunRefused(f"{self.stage}: queue poll failed, HTTP {status}")
            job = (data.get("jobs") or [{}])[0]
            if job.get("status") in ("done", "failed"):
                break
            if self.clock() >= deadline:
                raise RunRefused(f"{self.stage}: job {debate_id} timed out")
            self.sleep(self.poll_seconds)
        if job.get("status") != "done":
            raise RunRefused(
                f"{self.stage}: job {debate_id} failed: {str(job.get('error'))[:200]}"
            )

        try:
            result = json.loads(job.get("result_json") or "")
        except ValueError:
            raise RunRefused(f"{self.stage}: unreadable result_json") from None
        lane, model = result.get("served_lane"), result.get("served_model")
        self.jobs.append(
            {
                "stage": self.stage,
                "debate_id": debate_id,
                "served_lane": lane,
                "served_model": model,
            }
        )
        # These are labels the worker wrote. They do not prove which model answered:
        # the claude lane's served_model echoes the request, and agy's envelope names none.
        if lane != self.want_lane or model != self.model:
            raise RunRefused(
                f"{self.stage}: served {lane!r}/{model!r}, required {self.want_lane!r}/{self.model!r}"
            )
        out = _json_from_text(str(result.get("text") or ""))
        if not isinstance(out, dict):
            raise RunRefused(f"{self.stage}: answer was not a JSON object")
        return out


def _render_prompt(stage: str, ctx: Dict[str, Any]) -> Tuple[str, str]:
    """(system, user) built ONLY from what the stage context holds."""
    if stage == "ideation":
        user = f"SEED:\n{ctx['seed']}"
        if ctx.get("blocker_question"):
            user += f"\n\nBLOCKER QUESTION:\n{ctx['blocker_question']}"
        return ctx["system"], user
    user = (
        "IDEATION:\n"
        + json.dumps(ctx["ideation"], indent=2, default=str)
        + "\n\nRESEARCH:\n"
        + json.dumps(ctx["research"], indent=2, default=str)
    )
    if ctx.get("swarm_context"):
        user += "\n\nCONTEXT FILES:\n" + "\n\n".join(
            f"--- {p} ---\n{t}" for p, t in ctx["swarm_context"].items()
        )
    return ctx["system"], user


def _json_from_text(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i : j + 1])
        except ValueError:
            pass
    return None


# -- research ---------------------------------------------------------------

RESEARCH_INSTRUCTIONS = (
    "You are a research assistant with web search and web fetch tools. Search the web for "
    "the query below and fetch the pages you rely on. Cite only pages you actually fetched. "
    "Reply with ONE JSON object and nothing else: "
    '{"findings": "<what the sources say>", "urls": ["<fetched url>", ...]}. '
    "If you cannot search the web, reply with "
    '{"findings": "", "urls": [], "search_failed": true}. '
    "If you searched and found nothing relevant, reply with "
    '{"findings": "", "urls": [], "found_nothing": true}. '
    "Never answer from memory: a finding without a fetched source must not be reported."
)

_NO_SEARCH_RE = re.compile(
    r"(can(?:no|')?t|could not|unable to|do(?:es)? not|don't|no)\b[^.]{0,40}\b"
    r"(web ?search|search the web|web access|internet access|browse)",
    re.IGNORECASE,
)


def resolve_claude_bin() -> str:
    """Fail-closed (#823): a missing binary raises BinaryNotFound, never a guessed path."""
    env = os.environ.get("CLAUDE_BIN")
    if env:
        if os.path.isfile(env) or shutil.which(env):
            return env
        raise BinaryNotFound(f"$CLAUDE_BIN={env!r} is not an executable file")
    home_local = Path.home() / ".local" / "bin" / "claude"
    usr_local = Path("/usr/local/bin/claude")
    for p in (home_local, usr_local):
        if p.exists():
            return str(p)
    return which_or_raise(
        "claude", env_var="CLAUDE_BIN", checked=(str(home_local), str(usr_local))
    )


def research_argv(prompt: str, claude_bin: Optional[str] = None) -> List[str]:
    return [
        claude_bin or resolve_claude_bin(),
        "-p",
        prompt,
        "--model",
        RESEARCH_MODEL,
        "--tools=WebSearch,WebFetch",
        "--allowedTools=WebSearch,WebFetch",
        "--strict-mcp-config",
        '--mcp-config={"mcpServers":{}}',
        "--setting-sources=",
    ]


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_DNS_RE = re.compile(
    r"name or service not known|nodename nor servname|getaddrinfo|"
    r"temporary failure in name resolution|no address associated",
    re.IGNORECASE,
)


def _http_get(url: str, headers: dict, timeout: float, follow_redirects: bool):
    import httpx

    return httpx.get(
        url, headers=headers, timeout=timeout, follow_redirects=follow_redirects
    )


def _is_dns_failure(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, socket.gaierror) or _DNS_RE.search(str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


def url_status(url: str, get: Optional[Callable[..., Any]] = None) -> str:
    """'live' (<400), 'dead' (404, 410, DNS failure) or 'unverified' (everything else,
    401/403/429/5xx/timeouts included, since a blocking site is not a missing page)."""
    if urlparse(url).scheme not in ("http", "https"):
        return "unverified"
    try:
        resp = (get or _http_get)(
            url, headers={"User-Agent": _BROWSER_UA}, timeout=15, follow_redirects=True
        )
    except Exception as exc:
        return "dead" if _is_dns_failure(exc) else "unverified"
    code = resp.status_code
    if code < 400:
        return "live"
    return "dead" if code in (404, 410) else "unverified"


class ResearchRunner:
    """One query, one ``claude -p`` with web tools only, in an empty cwd, API keys scrubbed."""

    def __init__(
        self,
        *,
        guard: JobGuard,
        run: Callable[..., Any] = subprocess.run,
        get: Optional[Callable[..., Any]] = None,
        claude_bin: Optional[str] = None,
    ):
        self.guard, self.run, self.get, self.claude_bin = guard, run, get, claude_bin

    def __call__(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self.guard.check()
        query = ctx["query"]
        try:
            argv = research_argv(
                RESEARCH_INSTRUCTIONS + "\n\n" + query, self.claude_bin
            )
        except BinaryNotFound as exc:
            raise RunRefused(f"research: {exc}") from None
        try:
            with tempfile.TemporaryDirectory() as empty:
                proc = self.run(
                    argv,
                    cwd=empty,
                    capture_output=True,
                    text=True,
                    timeout=RESEARCH_TIMEOUT,
                    env={**scrub_env_for_subprocess(), "IS_SANDBOX": "1"},
                )
        except subprocess.TimeoutExpired:
            raise RunRefused("research: claude -p timed out") from None
        except OSError as exc:
            raise RunRefused(f"research: could not run claude: {exc}") from None
        if proc.returncode != 0:
            raise RunRefused(f"research: claude -p exited {proc.returncode}")
        out = _json_from_text(proc.stdout or "")
        if (
            not isinstance(out, dict)
            or not isinstance(out.get("findings"), str)
            or not isinstance(out.get("urls"), list)
        ):
            raise RunRefused("research: unparseable answer")
        findings = out["findings"]
        urls = [u for u in out["urls"] if isinstance(u, str)]
        if (
            out.get("search_failed")
            or _NO_SEARCH_RE.search(findings)
            or (findings.strip() and not urls and not out.get("found_nothing"))
        ):
            raise RunRefused(
                "research: no web search was done; refusing an answer from memory"
            )
        kept, dropped, unverified = [], 0, 0
        for u in urls:
            s = url_status(u, self.get)
            if s == "dead":
                dropped += 1
                continue
            kept.append(u)
            unverified += s == "unverified"
        return {
            "query": query,
            "findings": findings,
            "urls": kept,
            "dropped_urls": dropped,
            "unverified_urls": unverified,
        }
