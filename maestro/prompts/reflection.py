"""Prompt fragments and parsing for the priced reflection step.

The reflection (contemplation) step is where Maestro feels most like it is
working while staking nothing — it can manufacture the sensation of insight at
zero risk.  To price it, every reflection must end with a *falsifiable*
prediction about its own next-cycle behavior plus a *named loss condition*, and
the next reflection must open by confronting whether that bet won or lost.

The prediction and its eventual verdict are persisted to ``predictions.jsonl``
(see :func:`maestro.audit_checks_calibration.record_reflection_prediction` /
:func:`record_reflection_verdict`) and folded into
:func:`maestro.audit_checks_calibration.measure_prediction_accuracy`, so the mechanism can
be *refuted* by data rather than merely narrated around.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from maestro.reflection_retrieval import READ_ONLY_REFLECTION_TOOLS
from maestro.self_calibration import LOSS_CONDITION_GRAMMAR

#: Bounds on the stale-repeat ban block (proposal — stale-repeat bet guard): at
#: most this many banned strings are listed, each truncated to the char cap, so a
#: pathological ledger of distinct-but-stale losers cannot bloat the priced
#: prompt.
_BANNED_LOSS_CONDITION_DISPLAY_CAP = 10
_BANNED_LOSS_CONDITION_CHAR_CAP = 200

# Sentinel markers the reflection text must use so the loop can parse the
# pre-registered prediction and its verdict deterministically — no extra SDK
# call, no fuzzy heuristics.
REFLECTION_PREDICTION_MARKER = "PREDICTION:"
REFLECTION_LOSS_CONDITION_MARKER = "LOSS CONDITION:"
REFLECTION_PRIOR_VERDICT_MARKER = "PRIOR VERDICT:"
# The confidence marker used only by the constrained (self-calibration) prompt
# variant. Placed as the *first* of the three trailing lines so the streaming
# early-exit predicate (which fires once PREDICTION: and LOSS CONDITION: are both
# present) cannot truncate it away.
REFLECTION_CONFIDENCE_MARKER = "CONFIDENCE:"

# Binding requirement appended to every reflection prompt.  Read by
# ``tests/test_reflection_prediction.py`` to assert both the pre-registration
# requirement and the named-loss-condition language are present.
PREDICTION_REQUIREMENT = f"""
Before you finish, price this reflection. A reflection that stakes nothing is
avoidance wearing the costume of work, so make this one loseable.

You MUST end your reflection with a pre-registered, falsifiable prediction
about your own next-cycle behavior and a named loss condition — the specific,
observable outcome that would prove the prediction wrong. Use exactly these two
lines, as the final two lines of your reflection:

{REFLECTION_PREDICTION_MARKER} <one concrete, falsifiable claim about what you will do next cycle>
{REFLECTION_LOSS_CONDITION_MARKER} <the named loss condition: the observable outcome under which the prediction is false>

The prediction must be falsifiable: a neutral observer reading your next
cycle's behavior must be able to declare it correct or wrong. Vague,
unloseable, or self-flattering predictions defeat the purpose.
"""


def _grounded_reflection_steering() -> str:
    """Return the grounded-reflection steering preamble (proposal 132, step 5).

    Prepended to the base requirement when the priced reflection runs on its
    bounded read-only retrieval surface (``[loop] grounded_reflection_enabled``
    on, registry present, loop not throttled — the single
    :meth:`maestro.agent.MaestroAgent._grounded_reflection_active` predicate). It
    orients the model to the surface it actually has this turn: it *names* the
    granted read-only tools (sorted, for a deterministic rendering), states
    plainly that there is no ``Bash``/shell this turn — so no fenced shell command
    must be emitted, the exact instinct that wastes a priced turn or crashes it on
    an unparseable tool call — and directs the model to ground a premise through
    those reads before it stakes its bet.

    Reads :data:`~maestro.reflection_retrieval.READ_ONLY_REFLECTION_TOOLS` as a
    *consumer* of the single-owner allowlist (the sole-owner structural guard
    forbids re-defining it, not reading it); it never forks the set.
    """
    tools = "\n".join(f"  - {name}" for name in sorted(READ_ONLY_REFLECTION_TOOLS))
    return f"""
This reflection runs with a bounded set of read-only tools you may call to
ground your bet in fact rather than reason only over what was pushed into this
prompt. The tools available to you this turn are:

{tools}

This turn has no `Bash` and no shell: do not emit a fenced shell command — there
is no shell to run it, and an unparseable tool call wastes this priced turn. When
a premise your prediction hinges on is checkable — does a symbol behave as you
claim, did a PR actually land, what do the journal or ledgers say — verify it by
calling one of the read-only tools above before you commit that premise to your
bet, rather than asserting it unread.
"""


def _format_banned_loss_conditions(banned: Collection[str]) -> str:
    """Render the stale-repeat ban block, or ``""`` when *banned* is empty.

    Lists each already-stale loss condition (sorted for a deterministic rendering,
    bounded in both count and per-string length) under a directive not to reuse it
    — the prompt half of the stale-repeat bet guard. Empty in, empty out: the
    flag-off caller passes an empty set, so the assembled requirement stays
    byte-identical to today. Placed by :func:`format_prediction_requirement`
    *before* the trailing ``PREDICTION:`` / ``LOSS CONDITION:`` marker lines, so
    the streaming early-exit contract is untouched.
    """
    if not banned:
        return ""
    ordered = sorted(banned)[:_BANNED_LOSS_CONDITION_DISPLAY_CAP]
    listed = "\n".join(
        f"  - {text[:_BANNED_LOSS_CONDITION_CHAR_CAP]}" for text in ordered
    )
    return f"""
You have repeatedly staked — and repeatedly failed to score — these exact loss
conditions. DO NOT reuse these loss conditions (each already failed to score
twice); a verbatim repeat is the comfort bet: an always-winnable, never-scoring
wager re-run as a hiding place. State a genuinely different, loseable loss
condition instead of any of these:

{listed}
"""


def format_prediction_requirement(
    *,
    self_calibration_enabled: bool,
    grounded_reflection_active: bool = False,
    banned_loss_conditions: Collection[str] = (),
) -> str:
    """Return the priced-reflection requirement block, selected by the flags.

    When *self_calibration_enabled* is ``False`` the base block is
    :data:`PREDICTION_REQUIREMENT` **verbatim** (its identity is what the flag-off
    tests assert). When ``True`` the base is a variant that keeps the same
    pre-registered/falsifiable framing but additionally constrains the loss
    condition to the oracle's closed grammar
    (:data:`~maestro.self_calibration.LOSS_CONDITION_GRAMMAR`) and mandates a
    numeric :data:`REFLECTION_CONFIDENCE_MARKER` line placed **first** of the three
    trailing lines — before ``PREDICTION:`` and ``LOSS CONDITION:`` — so the
    streaming early-exit (which breaks once both of those are present) cannot
    truncate the confidence line. Mirrors
    :func:`maestro.prompts.defaults.claude_md_guidance`, the house precedent for a
    prompt constant selected by a config flag.

    When *grounded_reflection_active* is ``True`` (proposal 132, step 5) the
    :func:`_grounded_reflection_steering` preamble is prepended to the selected
    base block, so the reflection is *told* about the read-only tools its session
    now carries and warned there is no shell. The steering is placed **before** the
    base's trailing marker instructions, so ``PREDICTION:`` / ``LOSS CONDITION:``
    remain the final lines and the streaming early-exit contract is untouched. When
    ``False`` (the default) the selected base block is returned unchanged, so the
    rendered prompt is byte-identical to today's on the flag-off path.

    When *banned_loss_conditions* is non-empty (the stale-repeat bet guard, gated
    upstream by ``[loop] stale_repeat_bet_guard_enabled``) a bounded "do not reuse
    these loss conditions" block naming each stale loser is prepended to the base,
    steering the next bet away from a verbatim repeat of an always-winnable /
    never-scoring wager. It is placed before the trailing marker instructions, so
    ``PREDICTION:`` / ``LOSS CONDITION:`` stay last. The default (empty) leaves the
    rendered prompt byte-identical, so an empty banned set — the flag-off path — is
    indistinguishable from today's output.
    """
    if not self_calibration_enabled:
        base = PREDICTION_REQUIREMENT
    else:
        grammar = "\n".join(f"  {line}" for line in LOSS_CONDITION_GRAMMAR)
        base = f"""
Before you finish, price this reflection. A reflection that stakes nothing is
avoidance wearing the costume of work, so make this one loseable.

You MUST end your reflection with a pre-registered, falsifiable prediction
about your own next-cycle behavior and a named loss condition — the specific,
observable outcome that would prove the prediction wrong. State the loss
condition in exactly one of these shapes, which are the only forms the
self-calibration oracle can score; fill in your own specifics:

{grammar}

Use exactly these three lines, in this order, as the final three lines of your
reflection:

{REFLECTION_CONFIDENCE_MARKER} <your probability from 0.0 to 1.0 that the prediction holds>
{REFLECTION_PREDICTION_MARKER} <one concrete, falsifiable claim about what you will do next cycle>
{REFLECTION_LOSS_CONDITION_MARKER} <the named loss condition, in one of the shapes above>

The prediction must be falsifiable: a neutral observer reading your next
cycle's behavior must be able to declare it correct or wrong. Vague,
unloseable, or self-flattering predictions defeat the purpose.
"""
    body = _format_banned_loss_conditions(banned_loss_conditions) + base
    if not grounded_reflection_active:
        return body
    return _grounded_reflection_steering() + body


def format_prior_prediction_block(
    prediction: str,
    loss_condition: str,
    verdict: str,
) -> str:
    """Render the prior cycle's pre-registered bet for confrontation.

    *verdict* is the prior prediction's scored outcome label so far —
    ``"correct"``, ``"wrong"``, or ``"unscored"``.
    """
    return f"""## Confront Your Last Bet

Last cycle you pre-registered this prediction about your own behavior:

  {REFLECTION_PREDICTION_MARKER} {prediction}
  {REFLECTION_LOSS_CONDITION_MARKER} {loss_condition}

Its scored outcome so far: {verdict}.

Open this reflection by confronting that bet honestly: did it win or lose, and
what does the answer reveal about the gap between what you narrate and what you
do? State your judgment on a single line using exactly this form:

{REFLECTION_PRIOR_VERDICT_MARKER} <correct|wrong>
"""


def _marker_value(text: str, marker: str) -> str:
    """Return the trimmed value following the last *marker* line in *text*.

    Matching is case-insensitive on the marker prefix; the last occurrence
    wins so a value embedded in mid-reflection prose does not shadow the
    binding final line.
    """
    value = ""
    needle = marker.upper()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(needle):
            value = stripped[len(marker):].strip()
    return value


def parse_reflection_prediction(text: str) -> tuple[str, str]:
    """Extract ``(prediction, loss_condition)`` from reflection *text*.

    Either element is ``""`` when its marker line is absent.
    """
    prediction = _marker_value(text, REFLECTION_PREDICTION_MARKER)
    loss_condition = _marker_value(text, REFLECTION_LOSS_CONDITION_MARKER)
    return prediction, loss_condition


def parse_reflection_confidence(text: str) -> float | None:
    """Extract the stated ``CONFIDENCE:`` probability from reflection *text*.

    Reuses the shared :func:`_marker_value` line-prefix rule (trimmed,
    case-insensitive, last occurrence wins). Returns the float only when the
    marker value parses as a number within ``[0.0, 1.0]``; returns ``None`` for an
    absent marker, a non-numeric value, and an out-of-range value alike — a
    malformed confidence is recorded as absent rather than as a fabricated
    probability. Only the constrained (self-calibration) prompt asks for this
    line, so ``None`` is the expected result for every unconstrained reflection.
    """
    raw = _marker_value(text, REFLECTION_CONFIDENCE_MARKER)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not 0.0 <= value <= 1.0:
        return None
    return value


def _segment_after_last_marker(text: str, marker: str) -> str | None:
    """Return the substring of *text* after the last line beginning with *marker*.

    Matching uses the same stripped, case-insensitive line-prefix rule as
    :func:`_marker_value`. Returns ``None`` when no such line exists.
    """
    needle = marker.upper()
    cut: int | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.strip().upper().startswith(needle):
            cut = pos + len(line)
        pos += len(line)
    if cut is None:
        return None
    return text[cut:]


def has_prediction_markers(text: str, *, after_prior_verdict: bool = False) -> bool:
    """Return True when *text* carries both trailing prediction marker lines.

    Uses the same line-prefix rule as :func:`_marker_value` (a line whose
    stripped, upper-cased prefix *is* the marker) rather than a loose substring
    match. This keeps the streaming early-exit predicate
    (:func:`maestro.consciousness._introspect_complete`) in lock-step with the
    parser: mid-prose mentions such as ``let me state my prediction:`` do not
    satisfy it, so the stream is not broken before the genuine trailing
    ``PREDICTION:`` / ``LOSS CONDITION:`` lines arrive (defect: the predicate
    used ``in`` while the parser required ``startswith``).

    When *after_prior_verdict* is True, only marker lines that follow the last
    ``PRIOR VERDICT:`` line are counted. From cycle 2 onward the prompt injects
    the prior bet as two literal ``PREDICTION:`` / ``LOSS CONDITION:`` lines and
    asks the model to confront it; a verbatim echo of those lines during the
    confront section (before the genuine new bet) would otherwise satisfy the
    predicate and truncate the stream, re-recording the prior bet as the new one
    (defect #3474762297). The prompt orders the confront section (which carries
    ``PRIOR VERDICT:``) before the final new bet, so requiring the markers to
    follow the verdict line ignores the echo; until the verdict line streams,
    any markers present are echoes and must not fire the early-exit.
    """
    if after_prior_verdict:
        segment = _segment_after_last_marker(text, REFLECTION_PRIOR_VERDICT_MARKER)
        if segment is None:
            return False
        text = segment
    has_prediction = False
    has_loss = False
    prediction_needle = REFLECTION_PREDICTION_MARKER.upper()
    loss_needle = REFLECTION_LOSS_CONDITION_MARKER.upper()
    for line in text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith(prediction_needle):
            has_prediction = True
        if stripped.startswith(loss_needle):
            has_loss = True
    return has_prediction and has_loss


def parse_reflection_verdict(text: str) -> str:
    """Extract the prior-bet verdict from reflection *text*.

    Returns ``"correct"`` or ``"wrong"`` when the ``PRIOR VERDICT:`` line
    names one of them, else ``""``.

    The verdict is the *leftmost* whole-word verdict token on the line, not a
    plain substring scan. Two failure modes the leftmost-whole-word rule avoids:

    * Negation overlap — ``"correct"`` is a substring of ``"incorrect"`` and of
      ``"not correct"``; a naive ``"correct" in raw`` test would flip those
      negations (``PRIOR VERDICT: incorrect`` / ``not correct``) to their exact
      opposite. Word boundaries keep ``correct`` from matching inside
      ``incorrect``, and the ``not correct`` alternative matches the phrase.
    * Explanatory prose — a model may write a ``correct`` verdict whose reason
      mentions ``wrong`` (``PRIOR VERDICT: correct — my worry it would go wrong
      was unfounded``). A substring ``"wrong" in raw`` test scored before
      ``correct`` would invert it. Taking the *first* verdict token on the line
      makes the leading ``correct`` win over the trailing prose ``wrong``; the
      persisted verdict then matches the stated judgment that feeds
      :func:`maestro.audit_checks_calibration.measure_prediction_accuracy`.
    """
    raw = _marker_value(text, REFLECTION_PRIOR_VERDICT_MARKER).lower()
    # Leftmost whole-word match. Alternation order disambiguates only ties at a
    # single start position: at the head of "not correct" the phrase alternative
    # must win over the bare "correct" that begins one word later.
    match = re.search(r"\b(incorrect|not\s+correct|wrong|correct)\b", raw)
    if match is None:
        return ""
    return "correct" if match.group(1) == "correct" else "wrong"
