"""Card #1413 — pin live Gemini defaults in swarph_mesh adapters.

Fails if GeminiAdapter.default_model or GeminiCLIAdapter.default_model /
DEFAULT_MODEL reverts to gemini-2.5-*.
"""
from __future__ import annotations

import importlib
from unittest.mock import patch

import swarph_mesh.adapters.gemini as gemini_mod
import swarph_mesh.adapters.gemini_cli as gemini_cli_mod


def test_gemini_adapter_default_model_pinned():
    assert gemini_mod.GeminiAdapter.default_model == "gemini-3.8-flash"
    assert not gemini_mod.GeminiAdapter.default_model.startswith("gemini-2.5")


def test_gemini_cli_adapter_default_model_pinned():
    env = dict(gemini_cli_mod.os.environ)
    env.pop("GEMINI_SUBSCRIPTION_MODEL", None)
    with patch.dict("os.environ", env, clear=True):
        importlib.reload(gemini_cli_mod)
        assert gemini_cli_mod.DEFAULT_MODEL == "gemini-3.8-flash"
        assert gemini_cli_mod.GeminiCLIAdapter.default_model == "gemini-3.8-flash"
        assert not gemini_cli_mod.DEFAULT_MODEL.startswith("gemini-2.5")
