"""Pydantic structured-output harness per PLAN.md §7.

Implements the retry-once-with-[USER]-turn-feedback shape locked in
PR #125 (hedge-fund-mcp) + swarph_shared.json_mode.

We do NOT use ``swarph_shared.parse_json_with_retry`` directly here
because that helper takes a sync callback and our adapter's
``chat()`` is async. Bridging sync→async inside an already-running
event loop deadlocks. Instead we use ``swarph_shared.parse_json``
(the sync parse-only primitive) and orchestrate the retry in
async-native code.

Retry contract:
  1. Try parse on initial response.
  2. On parse failure: build a feedback string referencing the
     parse error, append it as a NEW ``[USER]`` turn, re-call the
     adapter, parse the second response.
  3. If retry response also fails to parse: error_class =
     ``"malformed_json"``.
  4. If the retry adapter call itself raises: error_class =
     ``"retry_failed"``.

The [USER]-turn invariant: feedback is a NEW user turn, not
concatenated onto the prior prompt. Concatenation drifts multi-turn
semantics; new turn preserves them.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from swarph_shared import build_retry_feedback_turn, parse_json

from swarph_mesh.types import ChatMessage

logger = logging.getLogger(__name__)


# Callback the harness needs from its caller: take feedback text,
# return the adapter's new response text.
RetryCallback = Callable[[str], Awaitable[str]]


async def parse_with_retry(
    initial_text: str,
    on_retry: RetryCallback,
) -> tuple[Optional[dict], Optional[str]]:
    """Async-native parse + retry-once.

    Returns ``(parsed, error_class)``:
      - ``(dict, None)`` on success
      - ``(None, "malformed_json")`` if both initial + retry fail
      - ``(None, "retry_failed")`` if the retry callback raised
    """
    # First attempt
    parsed, err = parse_json(initial_text)
    if parsed is not None:
        return parsed, None

    # Build feedback for retry
    feedback = build_retry_feedback_turn(err or "no JSON found in response")

    # Retry once
    try:
        retry_text = await on_retry(feedback)
    except Exception:
        logger.warning(
            "json_harness: retry callback raised; classifying as retry_failed",
            exc_info=True,
        )
        return None, "retry_failed"

    parsed, _ = parse_json(retry_text)
    if parsed is not None:
        return parsed, None
    return None, "malformed_json"


def make_retry_callback(
    adapter,
    base_messages: list[ChatMessage],
    model: str,
    system_prompt: Optional[str],
    temperature: float,
    max_tokens: Optional[int],
    accumulator: Optional[list] = None,
) -> RetryCallback:
    """Build the on_retry callback the harness expects.

    Appends feedback as a NEW user turn (per PR #125 invariant —
    NOT concatenated to the prior prompt). Recursion through the
    JSON harness is explicitly disabled by passing
    ``json_schema=None`` so a malformed retry can't infinite-loop.

    ``accumulator``: if provided, each retry's ``LLMResponse`` is appended so
    the caller can fold its tokens + cost into the returned response. The retry
    consumes real tokens/cost; without this they're invisible to attribution
    (cost dashboards undercount every schema call that needed a retry — sweep MED).
    """

    async def _on_retry(feedback: str) -> str:
        retry_messages = list(base_messages) + [
            ChatMessage(role="user", content=feedback),
        ]
        resp = await adapter.chat(
            messages=retry_messages,
            model=model,
            system_prompt=system_prompt,
            json_schema=None,  # No recursion — retry is plain-text only
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if accumulator is not None:
            accumulator.append(resp)
        return resp.text

    return _on_retry


__all__ = [
    "RetryCallback",
    "parse_with_retry",
    "make_retry_callback",
    "build_retry_feedback_turn",  # re-export for callers wanting the helper
]
