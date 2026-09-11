"""#823: a missing binary RAISES at resolve time, naming what was searched.

``shutil.which(...) or "<guess>"`` manufactured a plausible path when the
binary was absent; the exec then died in ~5ms as FileNotFoundError deep in
subprocess and read as a flaky worker (828 failures / 16 days, card #816).

The can-fail property: these tests FORCE the absent-binary state by patching
``shutil.which`` -> None and ``Path.home`` -> tmp_path, so they fail on the
defect even on a box where the binary happens to be installed — the exact
state (droplet has /usr/bin/firejail) in which the original bug was
invisible. A test that only runs where firejail exists cannot fail on this.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from swarph_mesh.adapters import antigravity, grok_cli, vibe_cli
from swarph_mesh.exceptions import AdapterError, BinaryNotFound

_ENV_VARS = ("AGY_BIN", "FIREJAIL_BIN", "GROK_BIN", "VIBE_BIN")

RESOLVERS = [
    (antigravity._resolve_agy_bin, "agy", "AGY_BIN"),
    (antigravity._resolve_firejail_bin, "firejail", "FIREJAIL_BIN"),
    (grok_cli._resolve_grok_bin, "grok", "GROK_BIN"),
    (grok_cli._resolve_firejail_bin, "firejail", "FIREJAIL_BIN"),
    (vibe_cli._resolve_vibe_bin, "vibe", "VIBE_BIN"),
    (vibe_cli._resolve_firejail_bin, "firejail", "FIREJAIL_BIN"),
]
_IDS = [f"{r[0].__module__.rsplit('.', 1)[-1]}-{r[1]}" for r in RESOLVERS]


@pytest.fixture
def absent_everywhere(monkeypatch, tmp_path):
    """The 'not installed' state, forced: no env override, PATH lookup
    misses, and a home dir holding no ~/.local binaries."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


@pytest.mark.parametrize("resolver,name,env_var", RESOLVERS, ids=_IDS)
def test_missing_binary_raises_naming_the_search(
    absent_everywhere, resolver, name, env_var
):
    """No manufactured path: absence raises, and the message carries its own
    diagnosis — the binary, the override var, and the PATH searched."""
    with pytest.raises(BinaryNotFound) as exc_info:
        resolver()
    msg = str(exc_info.value)
    assert name in msg
    assert env_var in msg
    assert "PATH=" in msg


def test_exception_satisfies_both_contracts(absent_everywhere):
    """The #823 falsifier demands a RuntimeError; the mesh contract demands
    the uniform AdapterError catch keeps working. BinaryNotFound is both."""
    with pytest.raises(BinaryNotFound) as exc_info:
        antigravity._resolve_firejail_bin()
    assert isinstance(exc_info.value, AdapterError)
    assert isinstance(exc_info.value, RuntimeError)


def test_agy_raise_names_the_home_local_it_probed(absent_everywhere):
    with pytest.raises(BinaryNotFound) as exc_info:
        antigravity._resolve_agy_bin()
    assert str(absent_everywhere / ".local" / "bin" / "agy") in str(exc_info.value)


@pytest.mark.parametrize("resolver,name,env_var", RESOLVERS, ids=_IDS)
def test_found_on_path_is_returned_not_overraised(
    monkeypatch, tmp_path, resolver, name, env_var
):
    """Fail-closed must not over-raise: a real PATH hit is returned as-is."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda n, *_a, **_k: f"/found/{n}")
    assert resolver() == f"/found/{name}"


def test_env_override_is_honored_verbatim(monkeypatch, tmp_path):
    """Operator override bypasses resolution entirely (existing contract):
    FIREJAIL_BIN is returned without an existence check, even when nothing
    is installed."""
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("FIREJAIL_BIN", "/operator/says/firejail")
    assert antigravity._resolve_firejail_bin() == "/operator/says/firejail"
    assert grok_cli._resolve_firejail_bin() == "/operator/says/firejail"
    assert vibe_cli._resolve_firejail_bin() == "/operator/says/firejail"


def test_existing_home_local_still_resolves_without_path(monkeypatch, tmp_path):
    """The existence-checked ~/.local/bin step stays first-class: a real
    file there resolves even when PATH lookup would miss."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    agy = bin_dir / "agy"
    agy.write_text("#!/bin/sh\n")
    assert antigravity._resolve_agy_bin() == str(agy)
