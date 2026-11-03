"""Clause 2 and the packaging of personas/swarph.md."""

import re
from pathlib import Path

import pytest

from swarph_mesh.skunkworks.persona import _split_h1_sections, load_persona

TECHNIQUES = (
    "hawkes, levy, lévy, brownian, homology, betti, mapper, riemannian, hyperbolic, "
    "lorentzian, geodesic, curvature, fisher, wasserstein, laplacian, hypergraph, copula, "
    "branching ratio, hodge, simplicial, manifold, tda, tailcor, diebold, corrgan, dspy"
).split(", ")


def banned_in(text):
    return [
        t
        for t in TECHNIQUES
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text, re.IGNORECASE)
    ]


def test_swarph_persona_loads_with_both_sections():
    p = load_persona("swarph")
    assert p.ideation_system and p.architect_system
    assert Path(p.source_path).name == "swarph.md"


def test_ideation_section_names_no_technique():
    """Clause 2. Mutation: add 'hawkes' to the ideation section -> this fails."""
    assert banned_in(load_persona("swarph").ideation_system) == []


def test_the_technique_test_can_fail():
    for term in (
        "Hawkes",
        "LÉVY",
        "branching  ratio".replace("  ", " "),
        "a Fisher metric",
        "TDA.",
    ):
        assert banned_in(f"use {term} here"), term
    assert banned_in("fishermen and mappers") == []  # whole words only
    base = load_persona("swarph").ideation_system
    assert banned_in(base + " Consider Wasserstein transport.") == ["wasserstein"]


def test_ideation_persona_content():
    t = load_persona("swarph").ideation_system.lower()
    assert "mesh" in t and "gateway" in t and "board" in t and "subscription" in t
    assert "speculat" in t and "costs one test" in t
    assert "real, running system" in t and "evidence" in t
    for shape in ("tooling", "coordination", "ops", "mathematical"):
        assert shape in t


def test_architect_persona_content():
    t = load_persona("swarph").architect_system
    assert "UNVALIDATED" in t and "APPROVED_FOR_BUILD" in t
    for k in ("verdict", "mechanism", "audit"):
        assert k in t


def test_persona_section_split_has_only_the_three_known_sections():
    text = (Path(load_persona("swarph").source_path)).read_text()
    assert set(_split_h1_sections(text)) == {
        "IDEATION_SYSTEM",
        "ARCHITECT_SYSTEM",
        "DEFAULTS",
    }


def test_persona_name_cannot_traverse(tmp_path):
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            load_persona(bad)
    with pytest.raises(FileNotFoundError):
        load_persona("nope")


def test_missing_required_section_raises(tmp_path):
    (tmp_path / "bad.md").write_text("# IDEATION_SYSTEM\nx\n")
    with pytest.raises(ValueError, match="ARCHITECT_SYSTEM"):
        load_persona("bad", base_dir=str(tmp_path))


def test_personas_ship_as_package_data():
    root = Path(__file__).resolve().parents[1]
    body = (root / "pyproject.toml").read_text()
    assert '"swarph_mesh.skunkworks" = ["personas/*.md"]' in body
    assert (root / "src/swarph_mesh/skunkworks/personas/swarph.md").is_file()
    assert (root / "src/swarph_mesh/skunkworks/personas/omega.md").exists() is False
