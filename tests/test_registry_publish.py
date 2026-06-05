"""Tests for the live indexer transport (registry/publish.py).

The publisher MUST read-merge-write: preserve a cell's existing capabilities, upsert
the feature by id, and POST the whole merged blob. These tests inject fake http
get/post so nothing touches a network.
"""
from swarph_mesh.registry import (
    OPENCLAW_SPEC,
    SKUNKWORKS_FEATURE,
    gateway_publisher,
    spec_to_feature,
)


def _fake_peer(caps):
    return {"name": "gemini-researcher", "url": "http://gr:8787", "capabilities": caps}


def test_publish_preserves_existing_capabilities_and_features():
    posted = {}
    existing = {"gpu": True, "claude_cli": "v2",
                "published_features": [{"id": "existing-feat", "cell": "gemini-researcher"}]}
    pub = gateway_publisher(
        "http://gw", "gemini-researcher", "tok",
        http_get=lambda url, tok: _fake_peer(existing),
        http_post=lambda url, body, tok: posted.update(body) or {"ok": True},
    )
    pub(SKUNKWORKS_FEATURE)

    caps = posted["capabilities"]
    assert caps["gpu"] is True and caps["claude_cli"] == "v2"   # other caps preserved
    ids = {f.get("id") for f in caps["published_features"]}
    assert "existing-feat" in ids                               # prior feature preserved
    assert "adversarial-council-pipeline" in ids               # new feature added
    assert posted["name"] == "gemini-researcher"               # registers under the right cell


def test_publish_is_idempotent_by_id():
    posted = {}
    state = {"capabilities": {"published_features": []}}

    def fake_get(url, tok):
        return _fake_peer(state["capabilities"])

    def fake_post(url, body, tok):
        state["capabilities"] = body["capabilities"]           # persist between calls
        posted.update(body)
        return {"ok": True}

    pub = gateway_publisher("http://gw", "gemini-researcher", "tok",
                            http_get=fake_get, http_post=fake_post)
    pub(SKUNKWORKS_FEATURE)
    pub(SKUNKWORKS_FEATURE)                                     # publish twice

    feats = posted["capabilities"]["published_features"]
    assert len([f for f in feats if f["id"] == "adversarial-council-pipeline"]) == 1


def test_skunkworks_feature_shape():
    f = SKUNKWORKS_FEATURE
    assert f["cell"] == "gemini-researcher"                     # caller-binding: her cell
    assert f["how_to_request"].count("LAUNCH_SKUNKWORKS") >= 1
    assert "skunkworks" in f["tags"]


def test_spec_to_feature_maps_adapterspec():
    feat = spec_to_feature(
        OPENCLAW_SPEC, description="d", what_it_does="w",
        how_to_request="r", tags=["t"], cell="lab-ovh",
    )
    assert feat["id"] == "openclaw"
    assert feat["cell"] == "lab-ovh"
    assert "cli" in feat["name"]
