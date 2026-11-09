"""Count final UTF-8 payloads and reserve a bounded context charge."""

from __future__ import annotations

import json

DEFAULT_BYTES = 8_000


class BudgetTooSmallError(ValueError):
    """The requested limit cannot hold the selected payload and metadata."""


BudgetTooSmall = BudgetTooSmallError


def compact_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def finalize(
    payload: dict,
    budget_bytes: int | None,
    remaining_context_bytes: int,
    *,
    truncated_count: int = 0,
) -> tuple[dict, int]:
    """Return exact payload size and a conservative charge for the same context.

    The conservative charge fixes the displayed remainder before measuring
    ``used_bytes``. This avoids a size/remainder oscillation at powers of ten.
    It over-reserves at most the metadata digit differences, reported separately
    as ``charged_bytes``. MCP and transport envelopes are outside this payload.
    """
    requested = DEFAULT_BYTES if budget_bytes is None else budget_bytes
    if type(requested) is not int or not 1 <= requested <= DEFAULT_BYTES:
        raise ValueError("budget_bytes must be an integer from 1 to 8000.")
    if (
        type(remaining_context_bytes) is not int
        or not 0 <= remaining_context_bytes <= DEFAULT_BYTES
    ):
        raise ValueError("Invalid remaining context budget.")
    if type(truncated_count) is not int or truncated_count < 0:
        raise ValueError("Invalid truncated_count.")
    limit = min(requested, remaining_context_bytes)
    result = {
        **payload,
        "used_bytes": limit,
        "budget": limit,
        "remaining_context_bytes": remaining_context_bytes,
        "remaining_bytes": remaining_context_bytes,
        "truncated_count": truncated_count,
        "charged_bytes": limit,
    }
    reserved = len(compact_bytes(result))
    if reserved > limit:
        raise BudgetTooSmall("The selected payload exceeds the available byte budget.")
    result["charged_bytes"] = reserved
    result["remaining_context_bytes"] = remaining_context_bytes - reserved
    result["remaining_bytes"] = remaining_context_bytes - reserved
    # With a fixed remainder, only the digit width of used_bytes can shrink.
    for _ in range(8):
        actual = len(compact_bytes(result))
        if result["used_bytes"] == actual:
            break
        result["used_bytes"] = actual
    if len(compact_bytes(result)) != result["used_bytes"] or result["used_bytes"] > reserved:
        raise ValueError("Could not finalize response accounting.")
    return result, reserved
