"""Goal decomposition and scope validation — breaks high-level goals into
concrete steps and validates that multi-file plans include per-file
verification."""

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from maestro.memory import PlanStep
from maestro.sdk import (
    QueryOptions,
    SDKError,
    _note_session_effort_override,
    json_complete,
    query,
    query_on_session,
    structured_output_fallback,
)

if TYPE_CHECKING:
    from maestro.providers.base import Session

MAX_STEP_DESCRIPTION = 2000

# Per-item caps keep individual step results short in the replan prompt.
_REPLAN_COMPLETED_RESULT_MAX_CHARS = 200
_REPLAN_FAILED_RESULT_MAX_CHARS = 500
# Total budget for the completed-steps block: bounds the replan prompt
# regardless of step count (context-bloat-caps invariant), eliding oldest-first.
_REPLAN_COMPLETED_TEXT_MAX_CHARS = 4000

_STEPS_OUTPUT_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["id", "description"],
                },
            },
        },
        "required": ["steps"],
    },
}


def _parse_steps(raw: Any) -> list[PlanStep]:
    """Parse LLM JSON output into a list of :class:`PlanStep`.

    Extracts ``id`` and ``depends_on`` when present.  Falls back to
    UUID-based IDs and empty dependency lists for old-format responses.
    A dict payload unwraps to its ``"steps"`` value when the key is
    present — an explicit non-list ``"steps"`` value yields no steps
    rather than falling back to a sibling list.  A dict result (no
    ``"steps"`` key, or a dict-valued one) unwraps to its first list
    value.  Returns an empty list when the data cannot be meaningfully
    parsed.
    """
    data = raw
    if isinstance(data, dict):
        data = data.get("steps", data)
        if isinstance(data, dict):
            list_values = [v for v in data.values() if isinstance(v, list)]
            if not list_values:
                return []
            data = list_values[0]

    if not isinstance(data, list):
        return []

    # First pass: collect steps with LLM-provided or generated IDs
    steps: list[PlanStep] = []
    seen_ids: set[str] = set()

    for s in data:
        if s is None:
            continue
        if isinstance(s, dict):
            raw_desc = s.get("description")
            llm_id = s.get("id")
            raw_deps = s.get("depends_on")
        else:
            raw_desc = s
            llm_id = None
            raw_deps = None

        desc = str(raw_desc) if raw_desc is not None else ""
        if not desc.strip():
            continue
        desc = desc[:MAX_STEP_DESCRIPTION]

        # Determine step_id: use LLM-provided id if valid and unique
        if isinstance(llm_id, str) and llm_id.strip() and llm_id.strip() not in seen_ids:
            step_id = llm_id.strip()
        else:
            step_id = uuid.uuid4().hex[:8]
        seen_ids.add(step_id)

        # Parse depends_on
        depends_on: list[str] = []
        if isinstance(raw_deps, list):
            depends_on = [str(d).strip() for d in raw_deps if isinstance(d, str) and str(d).strip()]

        steps.append(PlanStep(
            step_id=step_id,
            description=desc,
            depends_on=depends_on,
        ))

    # Second pass: drop dependencies that name no real step, and drop
    # self-references.
    valid_ids = {s.step_id for s in steps}
    for step in steps:
        resolved: list[str] = []
        for dep in step.depends_on:
            if dep in valid_ids and dep != step.step_id:
                resolved.append(dep)
        step.depends_on = resolved

    return steps


# --- Plan-scope gate --------------------------------------------------------
# Rejects multi-file plans that lack per-file verification steps.

# Matches common file-path patterns in step descriptions.
#
# Free-form LLM prose contains many dotted tokens that are NOT files:
# ``e.g.``, ``i.e.``, version strings (``1.2``, ``v1.4.0``), domains
# (``github.com``), ordinal references (``Section 4.2``), etc. To avoid
# spuriously counting these as files we require either:
#   - an embedded ``/`` (path-like form, e.g. ``maestro/planner.py``), OR
#   - an extension drawn from a curated allow-list of source/doc extensions
# The base filename must also be at least two characters, which excludes
# single-letter tokens like ``e.g`` and ``i.e``.
_FILE_EXT_ALLOW_LIST = (
    "py|md|toml|json|yaml|yml|js|ts|sh|html|css|txt"
)
_FILE_PATH_RE = re.compile(
    rf"""
    (?:^|[\s`"'(])                          # boundary: start, ws, or quote/paren
    (
        (?:[a-zA-Z0-9_.-]+/)+               # one or more directory segments
        [a-zA-Z0-9_-]{{2,}}                 # ≥2-char base filename
        \.[a-zA-Z0-9]{{1,10}}               # .extension (any when path-form)
        |
        [a-zA-Z0-9_-]{{2,}}                 # ≥2-char base filename
        \.(?:{_FILE_EXT_ALLOW_LIST})\b      # extension from allow-list
    )
    """,
    re.VERBOSE,
)

# Matches verification language in step descriptions.
_VERIFICATION_RE = re.compile(
    r"\b(?:tests?|tested|testing|verif(?:y|ies|ied|ying)|asserts?|asserted|asserting|"
    r"checks?|checked|checking|validates?|validated|validating|validation|"
    r"confirms?|confirmed|confirming)\b",
    re.IGNORECASE,
)

# Prose-doc files where the act of writing the prose IS the verification —
# they cannot be "tested" in any meaningful sense. Exempt from the
# verification-language requirement in ``check_plan_scope``.
_DOC_FILE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^CLAUDE\.md$"),
    re.compile(r"^README(?:\.[^/]+)?\.md$"),
    re.compile(r"^docs/.*\.md$"),
)


def _is_doc_only_file(path: str) -> bool:
    """Return True for prose-doc files exempt from the scope-gate
    verification requirement (CLAUDE.md, README*.md, docs/**/*.md)."""
    return any(rx.match(path) for rx in _DOC_FILE_RES)


def _file_referenced_in(description: str, file_path: str) -> bool:
    """Return ``True`` when ``description`` references ``file_path`` either
    by full path or by basename, both boundary-anchored.

    Checks one file in isolation, so it cannot tell same-basename files
    apart; canonical ``files_affected`` attribution goes through
    :func:`_referenced_canonical_files`, which requires shorthand to match a
    unique canonical entry.  Kept for the single paired-test lookup.
    """
    if not file_path:
        return False
    _BOUNDARY = r"""(?:^|[\s`"'(/])"""
    # Full-path match with leading-boundary anchor so that e.g.
    # ``foo.py`` does not falsely match inside ``myfoo.py``.
    if re.search(rf"{_BOUNDARY}{re.escape(file_path)}\b", description):
        return True
    # Basename match (e.g. ``planner.py`` matches a step that says
    # ``Verify planner.py end-to-end`` even when the canonical entry is
    # ``maestro/planner.py``).  Uses the same boundary anchor so a
    # basename like ``a.py`` doesn't match ``data.py``.
    base = file_path.rsplit("/", 1)[-1]
    if not base:
        return False
    return re.search(rf"{_BOUNDARY}{re.escape(base)}\b", description) is not None


# A maximal path-like token in step prose: not preceded by a path character,
# so ``prompts/foo.py`` inside ``maestro/prompts/foo.py`` is never a token.
_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_./-])[A-Za-z0-9_./-]+")


def _referenced_canonical_files(description: str, canonical: list[str]) -> set[str]:
    """Return the canonical files ``description`` references, resolved against
    the whole canonical set so same-basename files never cross-credit.

    An exact full-path match always credits that path.  A shorthand token (a
    bare basename or a partial path) credits canonical entry ``p`` only when
    ``p == token`` or ``p`` ends with ``/token`` *and* exactly one canonical
    entry satisfies that test — ambiguous shorthand credits nothing.  Tokens
    are maximal path runs, so the tail of a longer qualified path (the
    ``artifact_review.py`` in ``maestro/prompts/artifact_review.py``) is never
    read as shorthand for a different file.
    """
    found: set[str] = set()
    for raw in _PATH_TOKEN_RE.findall(description):
        token = raw.rstrip(".")
        if token in canonical:
            found.add(token)
            continue
        matches = [p for p in canonical if p.endswith("/" + token)]
        if len(matches) == 1:
            found.add(matches[0])
    return found


def _resolve_file_to_steps(
    steps: list[PlanStep],
    files_affected: list[str] | None = None,
) -> dict[str, list[int]]:
    """Map each touched file to the step indices that reference it.

    Single source of truth for the canonical (``files_affected``) vs.
    legacy-regex file-resolution rules shared by ``check_plan_scope``
    (distinct-file count) and ``_find_unverified_code_files`` (gap list).
    Includes doc files; doc-exemption filtering happens downstream.
    """
    file_to_steps: dict[str, list[int]] = {}
    if files_affected is not None:
        canonical = [f.strip() for f in files_affected if f and f.strip()]
        for path in canonical:
            file_to_steps.setdefault(path, [])
        for idx, step in enumerate(steps):
            for path in _referenced_canonical_files(step.description, canonical):
                file_to_steps[path].append(idx)
    else:
        for idx, step in enumerate(steps):
            for m in _FILE_PATH_RE.finditer(step.description):
                path = m.group(1)
                file_to_steps.setdefault(path, []).append(idx)
    return file_to_steps


# Minimum filename-stem length for the symbol-stem bridge — single/double
# letter stems (``a.py``, ``db.py``) map to symbols too short to attribute
# without sweeping in unrelated prose.
_MIN_SYMBOL_STEM_LEN = 4


def _paired_test_basename(file_path: str) -> str | None:
    """Return the paired test basename for a non-test ``.py`` source file.

    ``maestro/foo.py`` → ``test_foo.py``.  Returns ``None`` for a non-``.py``
    file or a file that is itself a test (stem already starts ``test_``), so a
    test file is never bridged to a paired test of its own.
    """
    base = file_path.rsplit("/", 1)[-1]
    if not base.endswith(".py"):
        return None
    stem = base[:-3]
    if not stem or stem.startswith("test_"):
        return None
    return f"test_{stem}.py"


def _stem_pascalcase_symbol(file_path: str) -> str | None:
    """Map a ``.py`` source file's filename stem to the PascalCase symbol it
    would define: ``memory.py`` → ``Memory``, ``task_queue.py`` → ``TaskQueue``.

    Returns ``None`` for a non-``.py`` file, a test file (stem starts
    ``test_``), or a stem shorter than :data:`_MIN_SYMBOL_STEM_LEN` — too
    short to attribute safely.
    """
    base = file_path.rsplit("/", 1)[-1]
    if not base.endswith(".py"):
        return None
    stem = base[:-3]
    if len(stem) < _MIN_SYMBOL_STEM_LEN or stem.startswith("test_"):
        return None
    parts = [p for p in stem.split("_") if p]
    if not parts:
        return None
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _symbol_or_test_verified(
    file_path: str,
    steps: list[PlanStep],
    canonical: list[str] | None = None,
) -> bool:
    """Return ``True`` when a non-doc, non-test code file is verified by a
    step that carries a verification verb *and* references the file either by
    its paired test file (``test_<stem>.py``) or by the PascalCase symbol
    derived from its filename stem.

    These two bridges only *add* attribution for a file the path/basename
    match (:func:`_file_referenced_in`) missed — a step must still carry a
    verification verb, so the per-file verification requirement is not
    weakened.  The symbol match is case-sensitive and whole-word (so the
    English word ``config`` cannot match ``Config`` and ``TomlConfig`` does
    not match ``Config``).  Returns ``False`` for a test/doc file, whose
    helpers yield no bridge candidates.

    When ``canonical`` is given, a paired-test basename or symbol that another
    non-doc canonical file also yields (two same-stem files) is ambiguous and
    grants no credit, so the bridges cannot restore cross-file attribution.
    """
    paired_test = _paired_test_basename(file_path)
    symbol = _stem_pascalcase_symbol(file_path)
    others = [
        p for p in (canonical or []) if p != file_path and not _is_doc_only_file(p)
    ]
    if paired_test and any(_paired_test_basename(p) == paired_test for p in others):
        paired_test = None
    if symbol and any(_stem_pascalcase_symbol(p) == symbol for p in others):
        symbol = None
    if paired_test is None and symbol is None:
        return False
    # Case-sensitive, whole-word symbol match.
    symbol_re = re.compile(rf"\b{re.escape(symbol)}\b") if symbol else None
    for step in steps:
        if not _VERIFICATION_RE.search(step.description):
            continue
        if paired_test and _file_referenced_in(step.description, paired_test):
            return True
        if symbol_re and symbol_re.search(step.description):
            return True
    return False


def _find_unverified_code_files(
    steps: list[PlanStep],
    files_affected: list[str] | None = None,
    file_to_steps: dict[str, list[int]] | None = None,
) -> list[tuple[str, str]]:
    """Identify code files lacking per-file verification language.

    Returns ``(file_path, best_step_text)`` pairs for every non-doc file
    whose mentioning steps lack verification keywords.  ``best_step_text``
    is the description of the first step that references the file (or
    ``"(not mentioned in any step)"`` when no step references it at all).

    This is a pure function reusing the same keyword set and doc-exemption
    rules as :func:`check_plan_scope`.  The gate delegates here instead of
    inlining the logic, and :func:`_replan_for_scope` calls this helper to
    build the concrete gap list injected into the replan prompt.

    File-resolution is delegated to :func:`_resolve_file_to_steps` (the
    single source of truth).  ``check_plan_scope`` may pass its already
    resolved ``file_to_steps`` map to avoid rebuilding it; when omitted the
    map is resolved here from ``steps``/``files_affected``.
    """
    if file_to_steps is None:
        file_to_steps = _resolve_file_to_steps(steps, files_affected)

    gaps: list[tuple[str, str]] = []
    for path, step_indices in sorted(file_to_steps.items()):
        if _is_doc_only_file(path):
            continue
        has_verification = any(
            _VERIFICATION_RE.search(steps[i].description)
            for i in step_indices
        )
        if not has_verification:
            # Bridge: a code file verified by the symbol it defines or by its
            # paired test file (in a verb-carrying step) still counts as
            # verified, even when no step names the file by path/basename.
            has_verification = _symbol_or_test_verified(
                path, steps, list(file_to_steps)
            )
        if not has_verification:
            best_text = (
                steps[step_indices[0]].description
                if step_indices
                else "(not mentioned in any step)"
            )
            gaps.append((path, best_text))

    return gaps


def check_plan_scope_detailed(
    steps: list[PlanStep],
    files_affected: list[str] | None = None,
) -> tuple[bool, str, list[tuple[str, str]]]:
    """Validate that multi-file plans include per-file verification.

    Returns ``(True, "", [])`` when the plan passes the gate:
      - Single-file (or zero-file) plans always pass.
      - Multi-file plans pass only when every touched file appears in at
        least one step whose description contains verification language
        (test/verify/assert/check/validate/confirm).  A step credits a file
        by its exact path, or by shorthand (basename / partial path) only
        when that shorthand matches a unique canonical file.
      - Prose-doc files (CLAUDE.md, README*.md, docs/**/*.md) are exempt
        from the verification requirement — updating the prose is itself
        the verification.

    Returns ``(False, reason, gaps)`` when the gate rejects the plan,
    where ``gaps`` is the full ``list[tuple[str, str]]`` from
    ``_find_unverified_code_files`` (each pair is an uncovered file path
    and the best matching step text).  Surfacing the gap list lets the
    caller reuse it (e.g. to build the replan prompt's per-file
    enumeration) instead of recomputing the same regex walk.

    When ``files_affected`` is provided it is used as the canonical file
    set (the structured ``EvolutionPlan.files_affected`` list).  Step
    descriptions are scanned only to attribute verification language to
    each canonical file by full-path or basename match.  This avoids the
    free-form regex misclassifying tokens like ``e.g.``, ``1.2`` or
    ``github.com`` as files, and conversely avoids missing files that the
    LLM declares structurally but never spells in prose.

    When ``files_affected`` is ``None`` the function falls back to the
    legacy regex-only path for backward compatibility.
    """
    # Determine total distinct files (including docs) for the single-file
    # exemption check — must happen before _find_unverified_code_files
    # filters out doc files.  The unfiltered map comes from the shared
    # resolver so the count and the gap list share one resolution.
    file_to_steps = _resolve_file_to_steps(steps, files_affected)

    if len(file_to_steps) <= 1:
        return True, "", []

    gaps = _find_unverified_code_files(
        steps, files_affected, file_to_steps=file_to_steps,
    )

    if not gaps:
        return True, "", []

    uncovered = [path for path, _ in gaps]
    return (
        False,
        f"Plan-scope gate: multi-file plan lacks per-file verification "
        f"for: {', '.join(uncovered)}",
        gaps,
    )


def check_plan_scope(
    steps: list[PlanStep],
    files_affected: list[str] | None = None,
) -> tuple[bool, str]:
    """Thin wrapper over :func:`check_plan_scope_detailed`.

    Preserves the historical ``(ok, reason)`` signature for callers that
    do not need the gap list (e.g. the post-replan recheck and the
    existing unit tests).  See ``check_plan_scope_detailed`` for the gate
    semantics.
    """
    ok, reason, _ = check_plan_scope_detailed(steps, files_affected)
    return ok, reason


class Planner:
    async def decompose(
        self,
        goal: str,
        identity_context: str,
        context: str = "",
        session: Session | None = None,
        *,
        require_claude_md_update: bool = False,
        workspace_moment: str = "",
    ) -> list[PlanStep]:
        """Decompose a goal into concrete, executable steps.

        When *session* is provided, the call is sent on the existing session
        and ``effort`` / ``output_format`` have no effect — the session-level
        options configured at creation apply instead.

        Proposal 108: *require_claude_md_update* gates the doc-update nudge.
        When ``False`` (the default) the "make the final step a documentation
        update" caveat is dropped, so a decomposition is not pushed to spend a
        step on a CLAUDE.md edit the loop never reads; when ``True`` the legacy
        caveat is restored, folded into the step-count cap as one instruction.

        Proposal 130: *workspace_moment* is the conscious-access broadcast block
        for the ``task_execution`` consumer. When non-empty it is prepended once
        to the front of the assembled prompt (mirroring the front-prepend the
        other consumers use), so the task-execution planning decision operates on
        the same selected moment as contemplation, evolution and task generation.
        Spliced before the session / bare-``query()`` branch so a single splice
        covers both delivery paths. It is advisory prompt text only — it grants no
        capability, approves no plan, and consults no gate. Empty (the default,
        and the flag-off path) leaves the assembled prompt byte-identical.
        """
        if not goal or not str(goal).strip():
            raise ValueError("Goal must be a non-empty string")
        _note_session_effort_override("planner", "medium", session)
        doc_step_clause = (
            ", and when the goal involves code\n"
            "changes make the final step an update to relevant documentation\n"
            "(CLAUDE.md, README.md, or docstrings) to reflect what changed"
            if require_claude_md_update
            else ""
        )
        prompt = f"""Break down this goal into concrete steps that can each
be executed independently. Each step should be a clear, actionable task.

Goal: {goal}
{f"Additional context: {context}" if context else ""}

Reply with a JSON object containing a "steps" array:
{{"steps": [
  {{"id": "step_1", "description": "Step description", "depends_on": []}},
  {{"id": "step_2", "description": "Another step", "depends_on": ["step_1"]}}
]}}

Each step has:
- id: short unique identifier (e.g. "step_1", "step_2")
- description: what to do
- depends_on: list of step IDs that must complete before this step starts.
  Steps with no dependencies (or empty list) can run in parallel.
  Steps that write to the same files MUST depend on each other.

Keep it focused — 2 to 7 steps{doc_step_clause}. Each
step should be completable in a single execution. Don't over-plan."""

        # Conscious-access broadcast (proposal 130): prepend the active moment
        # once, before the branch below, so the same block reaches both the
        # session and bare-``query()`` delivery paths. Empty by default and on
        # the flag-off path, so the assembled prompt stays byte-identical.
        if workspace_moment:
            prompt = f"{workspace_moment}\n\n{prompt}"

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="planner",
                complete_on=json_complete,
            )
        else:
            try:
                result = await query(
                    prompt=prompt,
                    system=identity_context,
                    options=QueryOptions(
                        effort="medium",
                        caller="planner",
                        output_format=_STEPS_OUTPUT_FORMAT,
                    ),
                    complete_on=json_complete,
                )
            except SDKError as e:
                result = structured_output_fallback(e)
        try:
            raw = result.parse_json()
        except json.JSONDecodeError:
            return [PlanStep(step_id=uuid.uuid4().hex[:8], description=goal)]

        steps = _parse_steps(raw)
        return steps or [PlanStep(step_id=uuid.uuid4().hex[:8], description=goal)]

    async def replan(
        self,
        goal: str,
        identity_context: str,
        completed_steps: list[PlanStep],
        failed_step: PlanStep,
        context: str = "",
        session: Session | None = None,
        loop_detected: bool = False,
    ) -> list[PlanStep]:
        """Generate revised remaining steps after a step failure.

        Returns new steps that work around the failure, or an empty list
        if re-planning is not possible.

        When *session* is provided, the call is sent on the existing session
        and ``effort`` / ``output_format`` have no effect — the session-level
        options configured at creation apply instead.

        When *loop_detected* is ``True`` the failed step aborted because it
        repeated the same tool call in a loop (``StepResult.loop_detected``);
        an explicit loop-avoidance instruction is appended so the revised
        steps take a fundamentally different approach rather than repeating
        the looping action.
        """
        _note_session_effort_override("replan", "medium", session)
        completed_lines = [
            f"- [DONE] {s.description} → "
            f"{(s.result or '')[:_REPLAN_COMPLETED_RESULT_MAX_CHARS]}"
            for s in completed_steps
        ]
        # Keep the most recent steps (most relevant to recovery) within the
        # total budget, then restore chronological order.
        kept: list[str] = []
        budget = _REPLAN_COMPLETED_TEXT_MAX_CHARS
        for line in reversed(completed_lines):
            cost = len(line) + (1 if kept else 0)
            if cost > budget:
                break
            kept.append(line)
            budget -= cost
        kept.reverse()
        omitted = len(completed_lines) - len(kept)
        if omitted:
            kept.insert(0, f"- [... {omitted} earlier completed steps omitted ...]")
        completed_text = "\n".join(kept)
        failed_text = (
            f"- [FAILED] {failed_step.description} → "
            f"{(failed_step.result or 'Unknown error')[:_REPLAN_FAILED_RESULT_MAX_CHARS]}"
        )
        loop_avoidance = (
            "\n\nThe previous attempt aborted because it repeated the same "
            "tool call in a loop. The revised steps MUST take a fundamentally "
            "different approach rather than repeating that action."
            if loop_detected
            else ""
        )

        prompt = f"""A task step has failed. Generate revised remaining steps that
work around or recover from the failure.

Original goal: {goal}
{f"Additional context: {context}" if context else ""}

Completed steps:
{completed_text or "(none)"}

Failed step:
{failed_text}{loop_avoidance}

Reply with a JSON object containing a "steps" array of 2-5 revised steps:
{{"steps": [
  {{"id": "recovery_1", "description": "Step description", "depends_on": []}},
  ...
]}}

Each step has:
- id: short unique identifier
- description: what to do
- depends_on: list of step IDs that must complete first (empty = can run immediately).
  Steps that write to the same files MUST depend on each other.

Focus on what can still be accomplished. If the failure is
unrecoverable, return {{"steps": []}}."""

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="replan",
                complete_on=json_complete,
            )
        else:
            try:
                result = await query(
                    prompt=prompt,
                    system=identity_context,
                    options=QueryOptions(
                        effort="medium",
                        caller="replan",
                        output_format=_STEPS_OUTPUT_FORMAT,
                    ),
                    complete_on=json_complete,
                )
            except SDKError as e:
                result = structured_output_fallback(e)
        try:
            raw = result.parse_json()
        except json.JSONDecodeError:
            return []

        return _parse_steps(raw)
