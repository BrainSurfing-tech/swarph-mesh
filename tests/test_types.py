"""Smoke tests for the v0.0.1 typed substrate.

Phase 1 (Gemini adapter) tests will land alongside the implementation.
For v0.0.1 the only invariants worth pinning are:

1. Public surface is importable as advertised in __init__.
2. Pydantic shapes accept canonical inputs.
3. The LLMAdapter Protocol is structural (runtime-checkable) so a
   minimal stub with the right attributes passes isinstance().
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

import pytest

from swarph_mesh import (
    AdapterError,
    ChatMessage,
    LLMAdapter,
    LLMResponse,
    SwarphMeshError,
    UnknownProvider,
    __version__,
)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_version_exported():
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2


def test_exception_hierarchy():
    assert issubclass(AdapterError, SwarphMeshError)
    assert issubclass(UnknownProvider, SwarphMeshError)


# ---------------------------------------------------------------------------
# ChatMessage / LLMResponse pydantic shapes
# ---------------------------------------------------------------------------


def test_chat_message_minimal():
    m = ChatMessage(role="user", content="hi")
    assert m.role == "user"
    assert m.content == "hi"


def test_chat_message_requires_role_and_content():
    with pytest.raises(Exception):  # pydantic ValidationError
        ChatMessage(role="user")  # type: ignore[call-arg]


def test_llm_response_minimal():
    r = LLMResponse(text="hi", duration_s=0.1)
    assert r.text == "hi"
    assert r.duration_s == 0.1
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.cost_usd == 0.0
    assert r.cached is False
    assert r.parsed is None
    assert r.error_class is None


def test_llm_response_with_attribution():
    r = LLMResponse(
        text="result",
        duration_s=1.23,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0042,
        cached=True,
    )
    assert r.input_tokens == 100
    assert r.output_tokens == 50
    assert r.cost_usd == pytest.approx(0.0042)
    assert r.cached is True


def test_llm_response_parsed_dict():
    r = LLMResponse(text='{"a": 1}', duration_s=0.5, parsed={"a": 1})
    assert r.parsed == {"a": 1}


# ---------------------------------------------------------------------------
# LLMAdapter Protocol — runtime-checkable structural fit
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Minimal stub matching the LLMAdapter Protocol shape. Used to verify
    runtime_checkable isinstance() works as documented."""

    name = "stub"
    default_model = "stub-model-v1"

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        return LLMResponse(text="stub", duration_s=0.0)

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs: object,
    ) -> AsyncIterator[str]:
        if False:
            yield ""

    def cost_per_token(self, model: str) -> tuple[float, float]:
        return (0.0, 0.0)

    def list_models(self, *, ttl_seconds: int = 86400):
        # v0.6.0 architectural promotion — new Protocol method.
        return []


def test_stub_adapter_is_llm_adapter():
    """Structural Protocol — _StubAdapter has the right attributes
    even though it doesn't inherit from LLMAdapter."""
    assert isinstance(_StubAdapter(), LLMAdapter)


def test_object_without_required_attrs_is_not_adapter():
    class _Bad:
        pass

    assert not isinstance(_Bad(), LLMAdapter)


def test_partial_adapter_is_not_llm_adapter():
    """Partial implementation (missing default_model) fails the check."""

    class _Partial:
        name = "x"

        async def chat(self, *a, **kw):
            ...

        async def stream(self, *a, **kw):
            if False:
                yield ""

        def cost_per_token(self, model: str) -> tuple[float, float]:
            return (0.0, 0.0)

        def list_models(self, *, ttl_seconds: int = 86400):
            return []

    assert not isinstance(_Partial(), LLMAdapter)


# ---------------------------------------------------------------------------
# swarph-shared dependency wiring — sanity check
# ---------------------------------------------------------------------------


def test_swarph_shared_is_importable():
    """Phase 1 adapters depend on swarph-shared for caller_convention,
    subprocess_env, etc. Verify the dep is resolvable in the install
    environment."""
    import swarph_shared

    assert hasattr(swarph_shared, "validate_caller")
    assert hasattr(swarph_shared, "validate_node_name")
