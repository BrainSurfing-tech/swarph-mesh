"""Clause 12 (separation from trading code) and clause 10 (registry strings gone)."""

import ast
import re
import subprocess
from pathlib import Path

import pytest

import swarph_mesh.skunkworks as pkg

PKG_DIR = Path(pkg.__file__).parent
ROOT = Path(__file__).resolve().parents[1]
BANNED_IMPORTS = {
    "workers",
    "research",
    "database",
    "orchestrator",
    "omega_llm",
    "anthropic",
    "google",
    "neo4j",
    "openai",
    "hedge_fund_mcp",
    "hub",
}
BANNED_PREFIXES = ("langchain", "psycopg")
OVERRIDE_NAMES = re.compile(r"override", re.IGNORECASE)


def _trees():
    for f in sorted(PKG_DIR.glob("*.py")):
        yield f, ast.parse(f.read_text(encoding="utf-8"))


def _imported_roots(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            yield n.module


def test_banned_imports_check_can_fail():
    bad = ast.parse("import psycopg2\nfrom workers.x import y\nimport langchain_core")
    assert {r.split(".")[0] for r in _imported_roots(bad)} == {
        "psycopg2",
        "workers",
        "langchain_core",
    }


def test_nothing_from_the_trading_stack_or_metered_sdks_is_imported():
    seen = 0
    for f, tree in _trees():
        seen += 1
        for mod in _imported_roots(tree):
            root = mod.split(".")[0]
            assert root not in BANNED_IMPORTS and not root.startswith(
                BANNED_PREFIXES
            ), (f.name, mod)
            assert not mod.startswith(
                (
                    "swarph_mesh.adapters.gemini",
                    "swarph_mesh.adapters.openai",
                    "swarph_mesh.adapters.deepseek",
                    "swarph_mesh.adapters.grok",
                    "swarph_mesh.adapters.claude",
                )
            ), (f.name, mod)
    assert seen >= 8


def test_no_omega_or_timescale_strings():
    for f, tree in _trees():
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                low = n.value.lower()
                assert "omega" not in low and "timescale" not in low, (
                    f.name,
                    n.value[:60],
                )


def test_no_override_identifier_anywhere():
    """Clause 9: no function, argument, keyword, name or attribute called override."""
    for f, tree in _trees():
        for n in ast.walk(tree):
            names = []
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                names.append(n.name)
            if isinstance(n, ast.arg):
                names.append(n.arg)
            if isinstance(n, ast.keyword) and n.arg:
                names.append(n.arg)
            if isinstance(n, ast.Name):
                names.append(n.id)
            if isinstance(n, ast.Attribute):
                names.append(n.attr)
            for name in names:
                assert not OVERRIDE_NAMES.search(name), (f.name, name)


def test_override_check_can_fail():
    tree = ast.parse("def approve(x, override=False): ...")
    assert any(
        isinstance(n, ast.arg) and OVERRIDE_NAMES.search(n.arg) for n in ast.walk(tree)
    )


def test_no_metered_api_key_variable_is_read():
    """No lane reads a billing key; research scrubs them from the child."""
    for f, tree in _trees():
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert not n.value.endswith("_API_KEY"), (f.name, n.value)


def test_registry_protocol_strings_are_gone():
    # built from parts so this file does not itself contain the strings
    needles = ["LAUNCH_" + "SKUNKWORKS", "[SKUNKWORKS" + ":"]
    out = subprocess.run(
        ["git", "grep", "-nF", "-e", needles[0], "-e", needles[1]],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode > 1:
        pytest.skip("not a git checkout")
    hits = [
        l for l in out.stdout.splitlines() if "test_skunkworks_separation.py" not in l
    ]
    assert hits == [], hits
