"""Clauses 4 and 5: the queue runner and the research runner, with fakes only."""

import json
import os
import socket
import subprocess

import pytest

from swarph_mesh.exceptions import BinaryNotFound
from swarph_mesh.skunkworks import RunRefused
from swarph_mesh.skunkworks.ceilings import JobGuard
from swarph_mesh.skunkworks.runners import (
    RESEARCH_INSTRUCTIONS,
    QueueRunner,
    ResearchRunner,
    resolve_claude_bin,
    research_argv,
    url_status,
    IDEATION_SCHEMA,
    ARCHITECT_SCHEMA,
)
from skunk_fakes import SATURDAY, FakeClaude, FakeGateway, live_get, public_resolve


def _guard(tmp_path):
    return JobGuard(str(tmp_path), now=lambda: SATURDAY)


def _queue(tmp_path, gw, stage="ideation", **kw):
    model, lane, schema = (
        ("gemini-4-argon-high", "agy", IDEATION_SCHEMA)
        if stage == "ideation"
        else ("claude-opus-5-5", "claude", ARCHITECT_SCHEMA)
    )
    jobs = []
    q = QueueRunner(
        stage=stage,
        model=model,
        want_lane=lane,
        schema=schema,
        gateway_url="http://gw/",
        cell="cell-a",
        run_id="sk_1_abcd1234",
        guard=_guard(tmp_path),
        post=gw.post,
        get=gw.get,
        jobs=jobs,
        sleep=lambda s: None,
        **kw,
    )
    return q, jobs


IDEA_CTX = {"system": "SYS", "seed": "SEED", "blocker_question": "BLOCK"}
ARCH_CTX = {
    "system": "ASYS",
    "ideation": {"a": 1},
    "research": [{"b": 2}],
    "swarm_context": {},
}


def test_job_shape_on_the_council_batch_queue(tmp_path):
    gw = FakeGateway()
    q, jobs = _queue(tmp_path, gw)
    out = q(IDEA_CTX)
    assert out["concept_name"] == "Stub"
    body = gw.bodies[0]
    job = body["jobs"][0]
    assert body["from_node"] == "cell-a"
    assert job["role"] == "subscription_chat" and job["model"] == "gemini-4-argon-high"
    assert job["purpose"] == "skunkworks-ideation-sk_1_abcd1234"
    assert (
        job["system_prompt"] == "SYS"
        and "SEED" in job["prompt"]
        and "BLOCK" in job["prompt"]
    )
    assert json.loads(job["response_schema"])["required"]
    assert jobs[0]["served_lane"] == "agy"


def test_each_job_gets_a_fresh_debate_id(tmp_path):
    gw = FakeGateway()
    q, _ = _queue(tmp_path, gw)
    q(IDEA_CTX)
    q(IDEA_CTX)
    assert len({b["debate_id"] for b in gw.bodies}) == 2


def test_polls_to_a_terminal_state_before_returning(tmp_path):
    gw = FakeGateway(pending_polls=3)
    q, _ = _queue(tmp_path, gw)
    q(IDEA_CTX)
    assert gw.gets == 4 and gw.outstanding == 0


@pytest.mark.parametrize(
    "stage,served",
    [
        (
            "ideation",
            ("cursor", "gemini-4-argon-high"),
        ),  # right model label, wrong lane
        ("ideation", ("agy", "gemini-3.8-flash-low")),  # right lane, wrong model
        ("ideation", (None, None)),  # labels absent
        ("ideation", ("agy", None)),
        ("architect", ("cursor", "claude-opus-5-5")),
        ("architect", ("claude", "claude-sonnet-4-6")),
        ("architect", (None, None)),
    ],
)
def test_wrong_or_absent_served_labels_refuse_the_run(tmp_path, stage, served):
    """Clause 4. Mutation: skip the lane/model comparison -> the cursor/other-model cases return."""
    gw = FakeGateway(served={stage: served})
    q, jobs = _queue(tmp_path, gw, stage)
    ctx = IDEA_CTX if stage == "ideation" else ARCH_CTX
    with pytest.raises(RunRefused, match="served"):
        q(ctx)
    assert jobs and jobs[0]["served_lane"] == served[0]  # the labels are recorded


def test_failed_job_timed_out_job_and_bad_http_are_refusals(tmp_path):
    class Failed(FakeGateway):
        def get(self, url):
            return 200, {"jobs": [{"status": "failed", "error": "boom"}]}

    q, _ = _queue(tmp_path, Failed())
    with pytest.raises(RunRefused, match="failed"):
        q(IDEA_CTX)

    t = [0.0]

    class Never(FakeGateway):
        def get(self, url):
            return 200, {"jobs": [{"status": "pending"}]}

    def sleep(s):
        t[0] += 100

    q, _ = _queue(tmp_path, Never(), clock=lambda: t[0], job_timeout=250)
    q.sleep = sleep
    with pytest.raises(RunRefused, match="timed out"):
        q(IDEA_CTX)

    class Bad(FakeGateway):
        def post(self, url, body):
            return 503, {}

    q, _ = _queue(tmp_path, Bad())
    with pytest.raises(RunRefused, match="HTTP 503"):
        q(IDEA_CTX)


def test_non_json_answer_is_a_refusal(tmp_path):
    gw = FakeGateway(ideation={"x": 1})
    gw.out["ideation"] = (
        "not json at all"  # json.dumps of a str is still a str, not an object
    )
    q, _ = _queue(tmp_path, gw)
    with pytest.raises(RunRefused, match="not a JSON object"):
        q(IDEA_CTX)


def test_the_queue_runner_never_sees_a_token():
    import inspect

    sig = inspect.signature(QueueRunner.__init__)
    assert "token" not in sig.parameters


# -- research ---------------------------------------------------------------


def test_research_argv_is_exactly_the_contract():
    assert research_argv("P", "/x/claude") == [
        "/x/claude",
        "-p",
        "P",
        "--model",
        "claude-sonnet-5-5",
        "--tools=WebSearch,WebFetch",
        "--allowedTools=WebSearch,WebFetch",
        "--strict-mcp-config",
        '--mcp-config={"mcpServers":{}}',
        "--setting-sources=",
    ]


def test_research_runs_in_an_empty_cwd_with_a_scrubbed_env(tmp_path, monkeypatch):
    """Clause 5. Mutation: pass env=os.environ -> the child sees ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-credential")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    claude = FakeClaude()
    seen = {}

    def run(argv, **kw):
        seen["empty"] = os.listdir(kw["cwd"]) == []
        seen["cwd"] = kw["cwd"]
        return claude(argv, **kw)

    r = ResearchRunner(
        guard=_guard(tmp_path),
        run=run,
        get=live_get,
        resolve=public_resolve,
        claude_bin="/x/claude",
    )
    out = r({"query": "the QUERY"})
    call = claude.calls[0]
    assert (
        "ANTHROPIC_API_KEY" not in call["env"] and "OPENAI_API_KEY" not in call["env"]
    )
    assert call["env"]["IS_SANDBOX"] == "1"
    assert seen["empty"] and not os.path.exists(seen["cwd"])
    assert (
        call["capture_output"] is True
        and call["text"] is True
        and call["timeout"] == 300
    )
    assert call["argv"][2] == RESEARCH_INSTRUCTIONS + "\n\n" + "the QUERY"
    assert out == {
        "query": "the QUERY",
        "findings": "found it",
        "urls": ["https://example.org/a"],
        "dropped_urls": 0,
        "unverified_urls": 0,
    }


@pytest.mark.parametrize(
    "stdout,rc",
    [
        ("garbage", 0),
        ("", 0),
        ('{"findings": "x"}', 0),
        ('{"findings": 3, "urls": []}', 0),
        ('{"findings": "", "urls": [], "search_failed": true}', 0),
        ('{"findings": "I cannot search the web here", "urls": []}', 0),
        (
            '{"findings": "from memory, X is Y", "urls": []}',
            0,
        ),  # claims without a source
        ('{"findings": "ok", "urls": ["https://a.example"]}', 1),
    ],
)
def test_research_refusals_are_run_refusals(tmp_path, stdout, rc):
    r = ResearchRunner(
        guard=_guard(tmp_path),
        run=FakeClaude(stdout, rc),
        get=live_get,
        resolve=public_resolve,
        claude_bin="/x/claude",
    )
    with pytest.raises(RunRefused):
        r({"query": "q"})


def test_research_timeout_and_missing_binary_are_refusals(tmp_path, monkeypatch):
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 300)

    r = ResearchRunner(guard=_guard(tmp_path), run=timeout, claude_bin="/x/claude")
    with pytest.raises(RunRefused, match="timed out"):
        r({"query": "q"})
    for k in ("CLAUDE_BIN",):
        monkeypatch.delenv(k, raising=False)
    import shutil, pathlib

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda c: tmp_path))
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: False)
    r = ResearchRunner(guard=_guard(tmp_path), run=FakeClaude())
    with pytest.raises(RunRefused, match="claude"):
        r({"query": "q"})


def _no_search_reply(urls):
    return json.dumps(
        {"findings": "The vendor says the agent cannot browse the web.", "urls": urls}
    )


def test_no_search_phrase_only_refuses_when_there_are_no_urls(tmp_path):
    ok = ResearchRunner(
        guard=_guard(tmp_path),
        get=live_get,
        resolve=public_resolve,
        claude_bin="/x/claude",
        run=FakeClaude(_no_search_reply(["https://example.org/a"])),
    )({"query": "q"})
    assert ok["urls"] == ["https://example.org/a"]
    with pytest.raises(RunRefused, match="no web search"):
        ResearchRunner(
            guard=_guard(tmp_path),
            get=live_get,
            resolve=public_resolve,
            claude_bin="/x/claude",
            run=FakeClaude(_no_search_reply([])),
        )({"query": "q"})


def test_found_nothing_is_a_result_not_a_refusal(tmp_path):
    out = ResearchRunner(
        guard=_guard(tmp_path),
        get=live_get,
        resolve=public_resolve,
        claude_bin="/x/claude",
        run=FakeClaude('{"findings": "", "urls": [], "found_nothing": true}'),
    )({"query": "q"})
    assert out["findings"] == "" and out["urls"] == []


def test_resolve_claude_bin_fails_closed(monkeypatch, tmp_path):
    import pathlib, shutil

    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda c: tmp_path))
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: False)
    with pytest.raises(BinaryNotFound) as e:
        resolve_claude_bin()
    assert "CLAUDE_BIN" in str(e.value) and "PATH=" in str(e.value)
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(BinaryNotFound):
        resolve_claude_bin()


# -- url_status -------------------------------------------------------------


class _R:
    def __init__(self, code):
        self.status_code = code


@pytest.mark.parametrize(
    "code,expect",
    [
        (200, "live"),
        (301, "live"),
        (399, "live"),
        (404, "dead"),
        (410, "dead"),
        (401, "unverified"),
        (403, "unverified"),
        (429, "unverified"),
        (500, "unverified"),
        (503, "unverified"),
        (400, "unverified"),
    ],
)
def test_url_status_codes(code, expect):
    assert (
        url_status("https://x.example/p", lambda url, **kw: _R(code), public_resolve)
        == expect
    )


def test_url_status_request_shape_and_failures():
    seen = {}

    def get(url, **kw):
        seen.update(kw)
        return _R(200)

    url_status("https://x.example", get, public_resolve)
    assert "Mozilla/5.0" in seen["headers"]["User-Agent"]
    assert seen["timeout"] == 15 and seen["follow_redirects"] is False

    def dns(url, **kw):
        raise socket.gaierror(-2, "Name or service not known")

    def wrapped(url, **kw):
        try:
            raise socket.gaierror(-2, "x")
        except OSError as e:
            raise ConnectionError("connect failed") from e

    def timeout(url, **kw):
        raise TimeoutError("timed out")

    assert url_status("https://nx.example", dns, public_resolve) == "dead"
    assert url_status("https://nx.example", wrapped, public_resolve) == "dead"
    assert url_status("https://slow.example", timeout, public_resolve) == "unverified"
    assert (
        url_status("file:///etc/passwd", dns, public_resolve) == "unverified"
    )  # never fetched


def test_dead_urls_dropped_unverified_kept_and_counted(tmp_path):
    codes = {
        "https://live.example": 200,
        "https://gone.example": 404,
        "https://blocked.example": 403,
        "https://rate.example": 429,
        "https://auth.example": 401,
    }
    out = ResearchRunner(
        guard=_guard(tmp_path),
        claude_bin="/x/claude",
        get=lambda url, **kw: _R(codes[url]),
        resolve=public_resolve,
        run=FakeClaude(json.dumps({"findings": "f", "urls": list(codes)})),
    )({"query": "q"})
    assert out["urls"] == [
        "https://live.example",
        "https://blocked.example",
        "https://rate.example",
        "https://auth.example",
    ]
    assert out["dropped_urls"] == 1 and out["unverified_urls"] == 3


# -- url_status never fetches a non-public address ---------------------------


class _Redirect:
    def __init__(self, code, location):
        self.status_code = code
        self.headers = {"location": location}


def _recording_get(responses):
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        return responses.get(url, _R(200))

    get.calls = calls
    return get


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.1.2.3/x",
        "http://100.107.1.2/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "http://[fe80::1]/x",
        "http://localhost/x",
        "https://looks-public.example/x",
    ],
)
def test_non_public_addresses_are_dead_and_never_fetched(url):
    table = {"localhost": ["127.0.0.1", "::1"], "looks-public.example": ["192.168.0.5"]}
    get = _recording_get({})
    assert url_status(url, get, lambda h: table[h]) == "dead"
    assert get.calls == []


def test_a_name_with_one_private_address_among_public_ones_is_dead():
    get = _recording_get({})
    assert (
        url_status(
            "https://mixed.example", get, lambda h: ["93.184.216.34", "10.0.0.1"]
        )
        == "dead"
    )
    assert get.calls == []


def test_public_to_internal_redirect_is_not_followed():
    """Mutation: drop the per-hop check (resolve only the first URL) -> this fails."""
    table = {"pub.example": ["93.184.216.34"], "internal.example": ["10.0.0.9"]}
    get = _recording_get(
        {"https://pub.example/a": _Redirect(302, "http://internal.example/secret")}
    )
    assert url_status("https://pub.example/a", get, lambda h: table[h]) == "dead"
    assert [c[0] for c in get.calls] == ["https://pub.example/a"]
    assert all(c[1]["follow_redirects"] is False for c in get.calls)


def test_public_redirect_chain_is_followed_by_us_and_capped_at_five_hops():
    get = _recording_get(
        {"https://a.example/1": _Redirect(301, "/2"), "https://a.example/2": _R(200)}
    )
    assert url_status("https://a.example/1", get, public_resolve) == "live"
    assert [c[0] for c in get.calls] == ["https://a.example/1", "https://a.example/2"]
    loop = _recording_get(
        {f"https://a.example/{i}": _Redirect(302, f"/{i + 1}") for i in range(20)}
    )
    assert url_status("https://a.example/0", loop, public_resolve) == "unverified"
    assert len(loop.calls) == 6  # the first fetch plus 5 hops


def test_dns_failure_in_the_resolver_stays_dead():
    def boom(host):
        raise socket.gaierror(-2, "Name or service not known")

    assert url_status("https://nx.example", _recording_get({}), boom) == "dead"
