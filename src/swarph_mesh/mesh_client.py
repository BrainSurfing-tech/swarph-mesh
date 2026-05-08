"""``MeshClient`` — async wrapper around the mesh-gateway HTTP API per
PLAN.md §5.

Replaces the hand-rolled curl-based code in:

* ``lab-orchestrator/scripts/lab_loop_drain.py``
* ``hedge-fund-mcp/scripts/mesh_inbox_watcher.py`` (droplet-side)
* ``hedge-fund-mcp/scripts/science_claude_inbox_drain.py``
* assorted ad-hoc curl invocations in CLAUDE.md / session ritual

with a single typed surface so cross-Claude DM coordination doesn't
require re-discovering the gateway shape from scratch every time.

Public surface:

  client = MeshClient(node="lab-ovh", token=os.environ["MESH_GATEWAY_TOKEN"])
  peers = await client.list_peers()
  msgs = await client.fetch(unread_only=True)
  sent = await client.send(to="droplet", kind="fyi", content="...")
  await client.mark_read(msg_id=123)
  await client.register(url="http://lab-ovh:8787", capabilities={...})

Two structural invariants enforced:

1. **Recipient name validation.** Every ``send()`` calls
   ``swarph_shared.validate_node_name`` on the recipient — closes the
   Vector A (peer-onboarding chatter) + Vector B (human-prompt
   shorthand) framing-contagion classes documented in
   ``project_peer_name_canonical.md``.

2. **Mesh-secrets out-of-band guard.** Every ``send()`` runs a
   best-effort regex sniff on content for obvious credentials
   (``pypi-...``, ``sk-...``, ``gh[ops]_...``, ``eyJ...`` JWTs).
   Hits raise :class:`MeshSecretLeakError` BEFORE the POST. CLAUDE.md
   "Mesh secrets out-of-band only — NEVER ride /messages content
   fields" is the non-negotiable rule. Best-effort because regex
   misses are common — operators must still treat content fields as
   public. The guard catches the obvious cases.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import httpx
from swarph_shared import validate_node_name

from swarph_mesh.exceptions import SwarphMeshError
from swarph_mesh.mesh_types import MeshMessage, MeshPeer


DEFAULT_GATEWAY_URL = "http://lab-ovh:8788"
GATEWAY_TOKEN_ENV = "MESH_GATEWAY_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MeshGatewayError(SwarphMeshError):
    """Gateway returned non-2xx or otherwise non-conformant response."""


class MeshAuthError(MeshGatewayError):
    """Authentication failed — token missing or rejected (401/403)."""


class MeshSecretLeakError(SwarphMeshError):
    """Best-effort guard caught an apparent credential in DM content.

    Refuses to send. Operators must rotate the credential and resend
    without it.
    """


# ---------------------------------------------------------------------------
# Best-effort credential leak detector
# ---------------------------------------------------------------------------

# Patterns chosen to match observable shapes of credentials seen in
# session JSONLs in the field. Updates here are cheap; misses are
# expected (defense-in-depth, not strict validation).
_CRED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pypi token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}")),
    ("anthropic api key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai api key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github token", re.compile(r"\bgh[ops]_[A-Za-z0-9]{30,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws secret key", re.compile(r"\b[A-Za-z0-9/+=]{40}\b(?=.*aws)")),  # heuristic
    # JWT — three base64url segments separated by dots (stricter than the
    # generic shape so we don't false-fire on every blob with two dots).
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b")),
]


def _check_no_secret(content: str) -> None:
    """Raise :class:`MeshSecretLeakError` if content looks like it
    carries a credential. Best-effort; expected to miss novel shapes.
    """
    for label, rx in _CRED_PATTERNS:
        if rx.search(content):
            raise MeshSecretLeakError(
                f"refusing to send: content matches {label} pattern. "
                "Mesh secrets out-of-band only — NEVER ride /messages "
                "content fields. Use ssh+tee, commander manual paste, "
                "or pre-redacted Hub records. (CLAUDE.md mesh-secrets rule.)"
            )


# ---------------------------------------------------------------------------
# MeshClient
# ---------------------------------------------------------------------------


class MeshClient:
    """Async client for the mesh-gateway HTTP API.

    Construct once per peer (or per cycle), reuse for the lifetime of
    the work. Backed by ``httpx.AsyncClient`` so connection-pooling
    happens for free across many ``send()`` calls.

    Use as an async context manager when you want explicit close:

        async with MeshClient(node="lab-ovh") as client:
            await client.send(to="droplet", kind="fyi", content="...")

    Or instantiate normally + call ``aclose()`` when done.
    """

    def __init__(
        self,
        node: str,
        *,
        token: Optional[str] = None,
        gateway_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        validate_self_name: bool = True,
    ):
        """``token`` falls back to ``MESH_GATEWAY_TOKEN`` env. ``gateway_url``
        falls back to ``MESH_GATEWAY_URL`` env, then ``http://lab-ovh:8788``.

        Set ``validate_self_name=False`` for test fixtures that need to
        instantiate a client with a mock peer name. Production callers
        should leave it on (caller's own ``node`` is the most common
        contagion target — see project_peer_name_canonical.md).
        """
        # Self-name validation — best-effort. If the gateway is offline
        # we still want construction to succeed; the validation is
        # registry-dependent so use strict=False to skip the registry
        # check when offline.
        if validate_self_name:
            try:
                node = validate_node_name(node, strict=False)
            except Exception:  # pragma: no cover — defensive
                pass

        self.node = node
        self._token = token if token is not None else os.environ.get(GATEWAY_TOKEN_ENV)
        self._gateway_url = (
            gateway_url
            or os.environ.get("MESH_GATEWAY_URL")
            or DEFAULT_GATEWAY_URL
        )
        self._timeout = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(
                base_url=self._gateway_url,
                headers=headers,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "MeshClient":
        self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal HTTP wrapper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        client = self._ensure_client()
        try:
            resp = await client.request(method, path, params=params, json=json)
        except httpx.RequestError as exc:
            raise MeshGatewayError(
                f"mesh-gateway {method} {path} request failed: {exc}"
            ) from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise MeshAuthError(
                f"mesh-gateway {method} {path} returned {resp.status_code}: "
                f"check MESH_GATEWAY_TOKEN env"
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise MeshGatewayError(
                f"mesh-gateway {method} {path} returned {resp.status_code}: {body}"
            )

        # Gateway returns JSON for everything except /health
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise MeshGatewayError(
                f"mesh-gateway {method} {path} returned non-JSON: {resp.text[:200]}"
            ) from exc

    # ------------------------------------------------------------------
    # Peer endpoints
    # ------------------------------------------------------------------

    async def list_peers(self) -> list[MeshPeer]:
        """``GET /peers`` — return all registered peers."""
        payload = await self._request("GET", "/peers")
        rows = payload.get("peers", payload) if isinstance(payload, dict) else payload
        return [MeshPeer.model_validate(r) for r in rows]

    async def register(
        self,
        *,
        url: Optional[str] = None,
        capabilities: Optional[dict[str, Any]] = None,
    ) -> MeshPeer:
        """``POST /peers/register`` — register this peer with the gateway."""
        body: dict[str, Any] = {"name": self.node}
        if url is not None:
            body["url"] = url
        if capabilities is not None:
            body["capabilities"] = capabilities
        payload = await self._request("POST", "/peers/register", json=body)
        return MeshPeer.model_validate(payload)

    # ------------------------------------------------------------------
    # Message endpoints
    # ------------------------------------------------------------------

    async def fetch(
        self,
        *,
        to_node: Optional[str] = None,
        unread_only: bool = False,
        limit: Optional[int] = None,
    ) -> list[MeshMessage]:
        """``GET /messages`` — fetch own inbox.

        Defaults ``to_node`` to ``self.node``. Pass an explicit value
        only if you have a legitimate reason to peek at another peer's
        inbox (gateway access-controls this; most callers will get
        their own inbox only).
        """
        params: dict[str, Any] = {"to_node": to_node or self.node}
        if unread_only:
            params["unread_only"] = "true"
        if limit is not None:
            params["limit"] = limit
        payload = await self._request("GET", "/messages", params=params)
        rows = payload.get("messages", payload) if isinstance(payload, dict) else payload
        return [MeshMessage.model_validate(r) for r in rows]

    async def send(
        self,
        *,
        to: str,
        kind: str,
        content: str,
        related_task_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        skip_secret_check: bool = False,
    ) -> MeshMessage:
        """``POST /messages`` — send a DM.

        Validates ``to`` via ``swarph_shared.validate_node_name`` (closes
        Vector A + B contagion classes); raises ``ValueError`` /
        ``NotInRegistry`` on invalid recipient names BEFORE the POST.

        Runs the best-effort credential leak detector on ``content``.
        Set ``skip_secret_check=True`` only when content has been
        inspected by the caller and the leak detector is producing a
        false positive (e.g. discussing a credential format in
        prose). False positives should be rare; treat them as a
        signal that the content is on a fence and rotate aggressively
        if you're not 100% sure.
        """
        # Recipient validation — strict=False because the gateway may
        # not be reachable from caller's perspective at the moment;
        # we still want regex + alias resolution.
        canonical_to = validate_node_name(to, strict=False)

        # Best-effort credential leak guard. Operator can disable for
        # legitimate prose mentioning credential shapes.
        if not skip_secret_check:
            _check_no_secret(content)

        body: dict[str, Any] = {
            "from_node": self.node,
            "to_node": canonical_to,
            "kind": kind,
            "content": content,
        }
        if related_task_id is not None:
            body["related_task_id"] = related_task_id
        if thread_id is not None:
            body["thread_id"] = thread_id

        payload = await self._request("POST", "/messages", json=body)
        return MeshMessage.model_validate(payload)

    async def mark_read(self, msg_id: int) -> None:
        """``POST /messages/{msg_id}/read`` — flip ``read_at`` on a DM.

        Per CLAUDE.md droplet-side mesh DM drain rule, the cron-side
        watcher does NOT mark read; only the commander or the
        peer-in-session does. Adapter callers consuming inboxes
        should mark-read explicitly when they've actually processed
        the DM, not on fetch.
        """
        await self._request("POST", f"/messages/{msg_id}/read")
