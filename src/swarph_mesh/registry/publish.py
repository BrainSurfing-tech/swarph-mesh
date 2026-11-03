"""Live indexer transport — publish a capability into the mesh-gateway feature graph.

The `/features` endpoint aggregates each cell's `capabilities.published_features`.
`POST /peers/register` UPSERTs `capabilities` with FULL REPLACE, so publishing a
feature MUST read-merge-write (GET the cell's registration → merge the feature into
`published_features` → POST the whole blob back) or it clobbers the cell's other
capabilities. This is the peer-register merge discipline, made reusable.

CALLER-BINDING NOTE: the gateway enforces caller-binding — a cell can only register
ITS OWN name. So `gateway_publisher` is for a cell publishing its own features; the
Skunkworks feature must be published BY the cell that runs it.
`SKUNKWORKS_FEATURE` is the ready artifact for the hosting cell to self-publish.

DURABILITY NOTE: a cell whose `capabilities` come from a daemon health-snapshot (e.g.
a host running its own daemon) will have a one-shot published_features POST overwritten on the daemon's next
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


# Reference TEMPLATE for the Skunkworks research pipeline. The cell that runs it
# self-publishes (the id is the original registry key, kept for discovery). It runs on subscription lanes through the swarph CLI.
SKUNKWORKS_FEATURE = {
    "id": "adversarial-council-pipeline",
    "name": "Skunkworks research pipeline",
    "description": (
        "Research pipeline on subscription lanes: ideation, web research, an architect "
        "audit, then a human gate. Approval produces no code."
    ),
    "what_it_does": (
        "Takes a seed and a board card, runs ideation and the architect on the mesh queue "
        "and research on the requesting cell's own Claude, and posts the results to the card."
    ),
    "how_to_request": (
        "swarph skunkworks run --card <N> --persona <P> --seed-file <F> --as <cell>; then "
        "status, show, stop and decide. Billing is subscription only (no metered API). "
        "Refused 09:30-16:00 New York time on weekdays."
    ),
    "runtime": "the requesting cell's process; no daemon",
    "tags": ["skunkworks", "research", "pipeline", "mesh-tool", "subscription", "template"],
    "cell": "<hosting-cell>",
}
