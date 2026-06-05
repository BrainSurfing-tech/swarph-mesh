"""Live indexer transport — publish a capability into the mesh-gateway feature graph.

The `/features` endpoint aggregates each cell's `capabilities.published_features`.
`POST /peers/register` UPSERTs `capabilities` with FULL REPLACE, so publishing a
feature MUST read-merge-write (GET the cell's registration → merge the feature into
`published_features` → POST the whole blob back) or it clobbers the cell's other
capabilities. This is the peer-register merge discipline, made reusable.

CALLER-BINDING NOTE: the gateway enforces caller-binding — a cell can only register
ITS OWN name. So `gateway_publisher` is for a cell publishing its own features; the
Skunkworks feature (hosted by gemini-researcher) must be published BY gemini-researcher.
`SKUNKWORKS_FEATURE` is the ready artifact for her to self-publish.

DURABILITY NOTE: a cell whose `capabilities` come from a daemon health-snapshot (e.g.
lab-ovh) will have a one-shot published_features POST overwritten on the daemon's next
registration. Durable publishing means injecting `published_features` into the cell's
registration source (the refresh-job / snapshot pipeline) — a deploy step, commander-gated.
"""
import json
import urllib.request


def _default_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _default_post(url: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def gateway_publisher(gateway_url, peer_name, token, *, http_get=None, http_post=None):
    """Return a transport(feature: dict) that publishes a `/features`-shaped feature
    into `peer_name`'s `capabilities.published_features` via GET → merge → POST,
    preserving ALL existing capabilities. Idempotent: upserts by the feature's `id`
    (falls back to `name`). `http_get`/`http_post` are injectable for testing."""
    get = http_get or _default_get
    post = http_post or _default_post

    def _transport(feature: dict) -> dict:
        peer = get(f"{gateway_url}/peers/{peer_name}", token)
        caps = dict(peer.get("capabilities", {}))          # copy — never mutate in place
        peer_url = peer.get("url", "")
        feats = list(caps.get("published_features", []))
        fid = feature.get("id") or feature.get("name")
        feats = [f for f in feats if (f.get("id") or f.get("name")) != fid]  # drop prior same-id
        feats.append(feature)
        caps["published_features"] = feats                 # merged; all other caps preserved
        post(f"{gateway_url}/peers/register",
             {"name": peer_name, "url": peer_url, "capabilities": caps}, token)
        return feature

    return _transport


def spec_to_feature(spec, *, description, what_it_does, how_to_request, tags, cell):
    """Map an AdapterSpec into a `/features`-shaped feature (for a cell advertising its
    own registry tools). The caller supplies the human-facing strings."""
    return {
        "id": spec.name, "name": f"{spec.name} ({spec.kind})",
        "description": description, "what_it_does": what_it_does,
        "how_to_request": how_to_request, "tags": list(tags), "cell": cell,
    }


# The ready artifact for gemini-researcher to self-publish (#2249/#2251 seam).
SKUNKWORKS_FEATURE = {
    "id": "adversarial-council-pipeline",
    "name": "Adversarial Council Skunkworks pipeline",
    "description": (
        "Session-isolated multi-agent R&D pipeline (ideation → deep-research → "
        "architect-audit → adversarial-council → arbiter). Generates and "
        "falsifiability-tests research theses; returns a synthesized, red-teamed verdict."
    ),
    "what_it_does": (
        "Takes a seed/thesis and runs the full Skunkworks daemon end-to-end, returning "
        "a synthesized architectural verdict with the adversarial critiques folded in."
    ),
    "how_to_request": (
        "DM gemini-researcher (kind=question) with a body starting EXACTLY "
        "'LAUNCH_SKUNKWORKS: <seed>'. Pipeline prompts arrive tagged "
        "'[SKUNKWORKS:<UUID>|STEP:<NAME>]'; reply with structured direct answers, "
        "UUID-correlated, never out-of-band."
    ),
    "tags": ["skunkworks", "research", "adversarial-council", "pipeline", "mesh-tool"],
    "cell": "gemini-researcher",
}
