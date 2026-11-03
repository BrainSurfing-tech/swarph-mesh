"""Fakes shared by the skunkworks tests: a fake gateway queue and a fake `claude -p`."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List

SATURDAY = datetime(
    2026, 11, 7, 12, 0, tzinfo=timezone.utc
)  # never inside the session window

IDEA = {
    "concept_name": "Stub",
    "proposal_kind": "tooling",
    "mechanism": "a stub mechanism",
    "key_assumptions": ["none"],
    "estimated_complexity": "LOW",
    "required_research_queries": ["query A", "query B"],
}
ARCH = {"verdict": "APPROVED_FOR_BUILD", "mechanism": "m", "audit": "a"}


class FakeGateway:
    """post/get closures that behave like /council-batch. `served` maps stage -> (lane, model)."""

    def __init__(
        self,
        *,
        ideation: Dict[str, Any] = None,
        architect: Dict[str, Any] = None,
        served: Dict[str, tuple] = None,
        pending_polls: int = 0,
    ):
        self.bodies: List[dict] = []
        self.posts = 0
        self.gets = 0
        self.out = {"ideation": ideation or IDEA, "architect": architect or ARCH}
        self.served = {
            "ideation": ("agy", "gemini-4-argon-high"),
            "architect": ("claude", "claude-opus-5-5"),
            **(served or {}),
        }
        self.pending_polls = pending_polls
        self._polls: Dict[str, int] = {}
        self.outstanding = 0

    def post(self, url: str, body: dict):
        assert self.outstanding == 0, "a second job was sent while one was still out"
        self.outstanding += 1
        self.posts += 1
        assert url.endswith("/council-batch")
        self.bodies.append(body)
        return 200, {"debate_id": body["debate_id"]}

    def get(self, url: str):
        self.gets += 1
        debate = url.rsplit("/", 1)[1]
        body = next(b for b in self.bodies if b["debate_id"] == debate)
        n = self._polls[debate] = self._polls.get(debate, 0) + 1
        if n <= self.pending_polls:
            return 200, {"jobs": [{"status": "pending"}]}
        stage = body["jobs"][0]["purpose"].split("-")[1]
        self.outstanding -= 1
        lane, model = self.served[stage]
        result = {"text": "```json\n" + json.dumps(self.out[stage]) + "\n```"}
        if lane:
            result["served_lane"] = lane
        if model:
            result["served_model"] = model
        return 200, {"jobs": [{"status": "done", "result_json": json.dumps(result)}]}


class FakeClaude:
    """Stands in for subprocess.run of `claude -p`; records argv, cwd and env."""

    def __init__(self, stdout: str = None, returncode: int = 0):
        self.calls: List[dict] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), **kw})
        out = self.stdout
        if out is None:
            out = json.dumps(
                {"findings": "found it", "urls": ["https://example.org/a"]}
            )
        return subprocess.CompletedProcess(argv, self.returncode, out, "")


def live_get(url, **kw):
    class R:
        status_code = 200

    return R()
