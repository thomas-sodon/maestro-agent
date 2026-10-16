"""Shared UTF-8 byte caps for task metadata rendered into prompts.

Context bloat caps (CLAUDE.md): ``TaskRecord.goal`` is persisted uncapped
(a PR-remediation lap stores its whole composed prompt as the goal), so every
renderer that injects task metadata into a prompt must bound it here rather
than re-deriving its own truncation.
"""

from __future__ import annotations

TRUNCATED_SENTINEL = "\n…[truncated]"

# Byte ceiling for a task goal rendered into any prompt.
MAX_TASK_GOAL_BYTES = 2000


def cap_utf8(text: str, max_bytes: int) -> str:
    """Head-truncate *text* to *max_bytes* UTF-8 bytes, sentinel included."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    keep = max(0, max_bytes - len(TRUNCATED_SENTINEL.encode("utf-8")))
    return encoded[:keep].decode("utf-8", errors="ignore") + TRUNCATED_SENTINEL
