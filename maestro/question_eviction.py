"""Sub-cap eviction accounting for ``open_questions`` resolution rows.

``Consciousness.save_self_model`` normalizes the model it persists, which
includes enforcing the ``open_questions`` sub-caps. A question present in the
model handed to that save but absent from the normalized model it returned was
dropped *mechanically* by a cap — the LLM never closed it.

``MaestroAgent._record_question_resolutions`` classifies each pair by diffing
the before/after question lists. Reading the pre-save list would record such a
question as ``kept`` even though it is gone; reading the post-save list alone
would record it as ``removed`` ("closed as answered"), which is factually wrong
and mutes rule-5 stall detection exactly the way the proposal-116
generative-debt case does. The correction is to read the post-save list *and*
feed the evicted keys into the same ``suppressed_keys`` channel proposal 116
already uses for a mechanically-withheld question, so they record ``kept``.
Both saves on the contemplate path cap ``open_questions`` — the
``update_self_model`` persist and contemplate's re-save — and the evictions
from both feed ``suppressed_keys`` (#4566).
"""

from __future__ import annotations

from collections.abc import Iterable

from maestro.consciousness import _normalize_question

__all__ = ["split_cap_eviction"]


def split_cap_eviction(
    pre_save: Iterable[object],
    after: Iterable[object],
) -> tuple[list[str], set[str]]:
    """Return the post-save questions and the keys the sub-cap evicted.

    The first element is *after* filtered to strings — the list
    ``_record_question_resolutions`` should diff against. The second is the
    set of normalized keys present in *pre_save* but absent from it. Non-string
    entries are ignored on both sides, mirroring the string-only reads every
    other ``open_questions`` consumer performs.
    """
    after_questions = [q for q in after if isinstance(q, str)]
    after_keys = {_normalize_question(q) for q in after_questions}
    evicted = {
        _normalize_question(q) for q in pre_save if isinstance(q, str)
    } - after_keys
    return after_questions, evicted
