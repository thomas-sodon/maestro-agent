"""Tests for stage-specific prompts."""

from __future__ import annotations

import string
from typing import Any

import pytest

from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    _PLAN_RATIONALE_MAX_CHARS,
)
from maestro.prompts.acceptance import (
    AcceptanceCriteriaStyle,
    format_acceptance_criteria_block,
)
from maestro.prompts.curate_plan import CURATE_PLAN_PROMPT
from maestro.prompts.defaults import (
    OBJECTIVE_PROMPT,
    TASK_GENERATION_PROMPT,
    TASK_GENERATION_PROMPT_EXTERNAL,
    claude_md_guidance,
    select_objective_prompt,
    select_task_generation_prompt,
)
from maestro.prompts.reflection import (
    PREDICTION_REQUIREMENT,
    REFLECTION_CONFIDENCE_MARKER,
    REFLECTION_LOSS_CONDITION_MARKER,
    REFLECTION_PREDICTION_MARKER,
    format_prediction_requirement,
)
from maestro.prompts.remediation import (
    MERGE_TRUTH_FIRST_PREAMBLE,
    merge_truth_first_preamble,
)
from maestro.reflection_retrieval import READ_ONLY_REFLECTION_TOOLS
from maestro.self_calibration import LOSS_CONDITION_GRAMMAR
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
)


def test_prompts_are_non_empty():
    for prompt in [
        OBJECTIVE_PROMPT,
    ]:
        assert isinstance(prompt, str)
        assert len(prompt) > 50


def test_objective_prompt_drops_stage_framing():
    """Proposals 113 / 149: the one objective framing names no stage goal."""
    assert "self-awareness and purpose" not in OBJECTIVE_PROMPT


def test_objective_prompt_requires_collision_naming_before_mint():
    """The identity prompt makes me name the shipped symbol I extend/collide.

    Targets the measured single-largest rejection mode (duplicate /
    premise-false-against-main): before authoring a proposal I must name the
    specific shipped symbol my change extends or collides with — or state none
    exists — and a plan duplicating a shipped/already-pending capability is
    called dead-on-arrival. This is the generation-time half, upstream of the
    priced propose_plan spend. ``OBJECTIVE_PROMPT`` is the injected system
    prompt, so the clause ships in every call.
    """
    assert (
        "name the specific shipped\nsymbol" in OBJECTIVE_PROMPT
        or "name the specific shipped symbol" in OBJECTIVE_PROMPT
    )
    assert "extends or would collide with" in OBJECTIVE_PROMPT
    assert "or state explicitly that none exists" in OBJECTIVE_PROMPT
    assert "dead-on-arrival" in OBJECTIVE_PROMPT


def test_claude_md_guidance_mandate_present_only_under_flag():
    """Proposal 108: the CLAUDE.md-maintenance mandate is now flag-selected.

    ``claude_md_guidance(True)`` restores the legacy hard mandate (the "Every
    evolution MUST update CLAUDE.md / Core Modules table" text); ``False``
    (the default) returns the soft, conditional guidance only. The hard
    mandate is no longer inlined in any prompt constant.
    """
    mandate = claude_md_guidance(True)
    assert "MUST update CLAUDE.md" in mandate
    assert "Core Modules table" in mandate

    soft = claude_md_guidance(False)
    assert "MUST update CLAUDE.md" not in soft
    assert "Core Modules table" not in soft
    assert "only if an invariant changed" in soft


def test_claude_md_guidance_default_flag_off_carries_no_mandate():
    """The runtime default (``Config().require_claude_md_update`` is False)
    resolves to the soft block byte-for-byte, and that block carries no
    ``REQUIREMENT: Every evolution MUST`` mandate heading (proposal 108
    flag-off guard, mirroring the remediation CI-section default guard).
    """
    from maestro.config import Config

    default = claude_md_guidance(Config().require_claude_md_update)
    assert default == claude_md_guidance(False)
    assert "REQUIREMENT: Every evolution MUST" not in default


def test_format_prediction_requirement_flag_off_is_identical():
    """Flag-off returns ``PREDICTION_REQUIREMENT`` by identity (byte-identical).

    Proposal 129 flag-off guard, mirroring
    ``test_claude_md_guidance_default_flag_off_carries_no_mandate``: the off-path
    must be the unchanged constant so the rendered priced-reflection prompt is
    byte-identical to before, and no self-calibration machinery leaks into it.
    """
    off = format_prediction_requirement(self_calibration_enabled=False)
    assert off == PREDICTION_REQUIREMENT
    assert REFLECTION_CONFIDENCE_MARKER not in off
    for line in LOSS_CONDITION_GRAMMAR:
        assert line not in off


def test_format_prediction_requirement_flag_on_carries_grammar_and_confidence():
    """Flag-on keeps the falsifiable framing, quotes every grammar line, and
    mandates ``CONFIDENCE:`` before the two markers so the stream cannot truncate
    it."""
    on = format_prediction_requirement(self_calibration_enabled=True)
    assert on != PREDICTION_REQUIREMENT
    assert "pre-registered, falsifiable prediction" in on
    assert REFLECTION_CONFIDENCE_MARKER in on
    for line in LOSS_CONDITION_GRAMMAR:
        assert line in on
    confidence_at = on.index(REFLECTION_CONFIDENCE_MARKER)
    prediction_at = on.index(REFLECTION_PREDICTION_MARKER)
    loss_at = on.index(REFLECTION_LOSS_CONDITION_MARKER)
    assert confidence_at < prediction_at < loss_at


@pytest.mark.parametrize("self_calibration_enabled", [False, True])
def test_format_prediction_requirement_grounded_off_is_base_variant(
    self_calibration_enabled: bool,
) -> None:
    """Proposal 132 step 5 flag-off guard: with ``grounded_reflection_active``
    omitted (its default ``False``), the selected base variant is returned
    unchanged for *both* self-calibration states — no read-only tool names, no
    ``Bash``/shell steering leak in, so the rendered prompt stays byte-identical to
    today's on every flag-off path.
    """
    base = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
    )
    grounded_off = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
        grounded_reflection_active=False,
    )
    assert grounded_off == base
    assert "Bash" not in grounded_off
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool not in grounded_off


@pytest.mark.parametrize("self_calibration_enabled", [False, True])
def test_format_prediction_requirement_grounded_on_names_tools_and_forbids_bash(
    self_calibration_enabled: bool,
) -> None:
    """Proposal 132 step 5 flag-on: the steering preamble names every allowlisted
    read-only tool, forbids ``Bash``/shell, composes with either self-calibration
    state (the base block — grammar lines included when on — is preserved intact at
    the end), and precedes the trailing ``PREDICTION:`` / ``LOSS CONDITION:``
    markers so the streaming early-exit contract is untouched.
    """
    base = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
    )
    on = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
        grounded_reflection_active=True,
    )
    # Steering is prepended: the whole base variant survives at the tail.
    assert on != base
    assert on.endswith(base)
    # Every allowlisted read-only tool is enumerated for orientation.
    assert len(READ_ONLY_REFLECTION_TOOLS) > 0
    for tool in READ_ONLY_REFLECTION_TOOLS:
        assert tool in on
    # The no-shell warning is present.
    assert "Bash" in on
    assert "no shell" in on
    # The steering sits before the trailing marker lines.
    steering_at = on.index("read-only tools")
    prediction_at = on.index(REFLECTION_PREDICTION_MARKER)
    loss_at = on.index(REFLECTION_LOSS_CONDITION_MARKER)
    assert steering_at < prediction_at
    assert steering_at < loss_at
    # The self-calibration grammar is still carried when that flag is on.
    if self_calibration_enabled:
        for line in LOSS_CONDITION_GRAMMAR:
            assert line in on


@pytest.mark.parametrize("self_calibration_enabled", [False, True])
def test_format_prediction_requirement_banned_empty_is_base(
    self_calibration_enabled: bool,
) -> None:
    """Stale-repeat guard flag-off proxy: an empty banned set is byte-identical.

    The upstream flag-off path passes an empty banned set, so the rendered
    requirement must equal the set-omitted base for both self-calibration states
    — no "do not reuse" block leaks in.
    """
    base = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
    )
    empty = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
        banned_loss_conditions=(),
    )
    assert empty == base
    assert "do not reuse" not in empty.lower()


@pytest.mark.parametrize("self_calibration_enabled", [False, True])
def test_format_prediction_requirement_banned_set_names_each_string(
    self_calibration_enabled: bool,
) -> None:
    """A non-empty banned set renders a block naming every banned loss condition.

    The block is prepended before the trailing ``PREDICTION:`` / ``LOSS
    CONDITION:`` markers so the streaming early-exit contract is untouched, and it
    composes with either self-calibration state (the base variant survives at the
    tail).
    """
    base = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
    )
    banned = ["pr #10 settles within 5 cycles", "gate wip_primacy does not fire"]
    on = format_prediction_requirement(
        self_calibration_enabled=self_calibration_enabled,
        banned_loss_conditions=banned,
    )
    assert on != base
    assert on.endswith(base)
    assert "do not reuse" in on.lower()
    for text in banned:
        assert text in on
    # The ban block precedes the trailing marker lines.
    ban_at = on.lower().index("do not reuse")
    assert ban_at < on.index(REFLECTION_PREDICTION_MARKER)
    assert ban_at < on.index(REFLECTION_LOSS_CONDITION_MARKER)


def test_task_generation_prompt_has_format_fields():
    """TASK_GENERATION_PROMPT must have the expected format fields."""
    for field in ("open_questions", "recent_tasks", "next_task_directive"):
        assert f"{{{field}}}" in TASK_GENERATION_PROMPT, (
            f"TASK_GENERATION_PROMPT missing format field '{field}'"
        )


def test_task_generation_prompt_acceptance_criteria_default_is_accurate():
    """The prompt must accurately describe the runtime default for
    acceptance_criteria. QueuedTask.acceptance_criteria is typed
    `list[str] | None` and defaults to None, not an empty list — so the
    prompt language must not claim the default is an empty list.
    """
    assert "defaults to an empty list" not in TASK_GENERATION_PROMPT
    assert "absent/null" in TASK_GENERATION_PROMPT


def _format_fields(template: str) -> set[str]:
    """Return the named ``{field}`` replacement fields in *template*.

    ``string.Formatter().parse`` treats ``{{``/``}}`` as literal braces (so the
    JSON-schema block is not mistaken for fields) and yields the real
    replacement-field names.
    """
    return {
        name
        for _, name, _, _ in string.Formatter().parse(template)
        if name is not None
    }


def test_external_task_generation_carries_all_self_fields_plus_target_repo():
    """Proposal 107: the external curator variant preserves every format field
    the self one interpolates, and adds ``{target_repo}`` (absent from self)."""
    self_fields = _format_fields(TASK_GENERATION_PROMPT)
    ext_fields = _format_fields(TASK_GENERATION_PROMPT_EXTERNAL)
    assert self_fields <= ext_fields, (
        f"external variant dropped fields: {self_fields - ext_fields}"
    )
    assert "target_repo" in ext_fields
    assert "target_repo" not in self_fields


def test_select_prompts_default_to_self_constants():
    """``self_evolution`` selectors return the self constants byte-for-byte,
    mirroring the ``claude_md_guidance`` default guard (``==``, since a sibling
    test reloads the module and rebinds the constant objects)."""
    assert select_task_generation_prompt("self_evolution") == TASK_GENERATION_PROMPT


def test_select_prompts_external_variants_substitute_repo():
    """The ``external_delivery`` selector returns the external variant."""
    assert (
        select_task_generation_prompt("external_delivery")
        == TASK_GENERATION_PROMPT_EXTERNAL
    )


def test_external_variants_drop_self_language():
    """The external variant drops the self-referential framing phrases."""
    for banned in ("for yourself", "your own evolution", "self-modification"):
        assert banned not in TASK_GENERATION_PROMPT_EXTERNAL


def test_curate_plan_prompt_has_format_fields():
    """CURATE_PLAN_PROMPT must expose every placeholder the curate phase fills."""
    for field in (
        "plan_id", "title", "created_at", "rationale", "proposed_changes",
        "acceptance_criteria", "rationale_max_chars", "change_item_max_chars",
        "obsolete_reason_max_chars", "acceptance_criteria_max_items",
        "acceptance_criteria_item_max_chars",
    ):
        assert f"{{{field}}}" in CURATE_PLAN_PROMPT, (
            f"CURATE_PLAN_PROMPT missing format field '{field}'"
        )


def test_curate_plan_prompt_states_response_shape_contract():
    """The prompt must spell out the action verdict contract."""
    for needle in ('"action"', "refine", "obsolete", "keep", '"reason"'):
        assert needle in CURATE_PLAN_PROMPT, (
            f"CURATE_PLAN_PROMPT missing response-shape token {needle!r}"
        )


def test_curate_plan_prompt_renders_target_plan_and_caps():
    """Rendering carries the target plan's title/rationale and the memory caps.

    The cap placeholders are filled from the ``maestro.memory`` constants so
    the prompt never drifts from the limits ``refine_plan`` / ``mark_plan_obsolete``
    actually enforce.
    """
    rendered = CURATE_PLAN_PROMPT.format(
        plan_id="plan_abc",
        title="Cache prompt fragments by content hash",
        created_at="2026-05-01T00:00:00+00:00",
        rationale="Repeated fragments recompute every call.",
        proposed_changes="- Hash fragments\n- Reuse on hit",
        acceptance_criteria="- A test asserts the cache hit path",
        files_affected="- maestro/cache.py",
        file_path_max_chars=_PLAN_FILE_PATH_MAX_CHARS,
        files_affected_max_items=_PLAN_FILES_AFFECTED_MAX_ITEMS,
        rationale_max_chars=_PLAN_RATIONALE_MAX_CHARS,
        change_item_max_chars=_PLAN_CHANGE_ITEM_MAX_CHARS,
        obsolete_reason_max_chars=_OBSOLETE_REASON_MAX_CHARS,
        acceptance_criteria_max_items=ACCEPTANCE_CRITERIA_MAX_ITEMS,
        acceptance_criteria_item_max_chars=ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    )
    assert "plan_abc" in rendered
    assert "Cache prompt fragments by content hash" in rendered
    assert "Repeated fragments recompute every call." in rendered
    # Assert the proposed_changes / acceptance_criteria / created_at values were
    # actually substituted. A substring/braces check alone gives a false positive
    # when a placeholder is accidentally doubled (``{{proposed_changes}}`` renders
    # as the literal token without raising), so pin the rendered body content too.
    assert "- Hash fragments" in rendered
    assert "- A test asserts the cache hit path" in rendered
    assert "files_affected:\n  - maestro/cache.py" in rendered
    assert '``"files_affected"``' in rendered
    assert str(_PLAN_FILE_PATH_MAX_CHARS) in rendered
    assert str(_PLAN_FILES_AFFECTED_MAX_ITEMS) in rendered
    assert "2026-05-01T00:00:00+00:00" in rendered
    assert str(_PLAN_RATIONALE_MAX_CHARS) in rendered
    assert str(_PLAN_CHANGE_ITEM_MAX_CHARS) in rendered
    assert str(_OBSOLETE_REASON_MAX_CHARS) in rendered
    assert str(ACCEPTANCE_CRITERIA_MAX_ITEMS) in rendered
    # #4104: the criterion cap is stated separately from the change cap.
    flat = " ".join(rendered.split())
    assert (
        f"Each ``proposed_changes`` item MUST be at most "
        f"{_PLAN_CHANGE_ITEM_MAX_CHARS} characters"
    ) in flat
    assert (
        f"Each ``acceptance_criteria`` item MUST be at most "
        f"{ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS} characters"
    ) in flat


def test_curate_plan_prompt_is_review_only():
    """The prompt must forbid the curator from approving/implementing plans."""
    assert "do not approve, reject, or implement" in CURATE_PLAN_PROMPT.lower()
    assert "human" in CURATE_PLAN_PROMPT.lower()


def _build_remediation_prompt() -> str:
    """Assemble a PR-remediation prompt (plan-less variant) for assertions.

    The composed prompt carries the owned-push preamble: ``AWAITING_PUSH`` is
    named only in the owned-push instruction (proposal 098).
    """
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    return _compose_pr_remediation_prompt(
        pr_number=2662,
        pr_title="Fix conflicts",
        plan=None,
        plan_id="plan_xyz",
        pr_body="(original body)",
        pr_url="https://example.invalid/pr/2662",
        problems=["The branch has a merge conflict against main."],
    )


def test_remediation_prompt_fetches_before_is_ancestor():
    """`git fetch` must appear BEFORE `--is-ancestor` in the assembled prompt.

    This is the load-bearing ordering: fetch authors the verdict, so the
    is-ancestor reading must come from a tree pulled THIS turn. Asserting the
    relative position guards against a future edit that reorders the two
    physical actions.
    """
    prompt = _build_remediation_prompt()
    fetch_idx = prompt.find("git fetch")
    ancestor_idx = prompt.find("--is-ancestor")
    assert fetch_idx != -1, "remediation prompt missing `git fetch`"
    assert ancestor_idx != -1, "remediation prompt missing `--is-ancestor`"
    assert fetch_idx < ancestor_idx, (
        "`git fetch` must precede `--is-ancestor` in the remediation prompt"
    )
    # The full merge-base form must be present, not just the bare flag.
    assert "git merge-base --is-ancestor HEAD origin/main" in prompt


def test_remediation_prompt_states_exit_zero_means_landed():
    """The preamble must tell the agent exit 0 means the cure already landed."""
    prompt = _build_remediation_prompt().lower()
    assert "exit code 0" in prompt
    assert "already landed" in prompt
    assert "do not re-resolve" in prompt


def test_remediation_prompt_exit_zero_reports_partial_never_success():
    """An already-landed head must report ``partial``, never ``success``.

    The exit-0 branch describes the exact condition
    :meth:`maestro.agent.Maestro._gate_pr_remediation_landed` evicts as a
    ``partial`` no-op: HEAD is already an ancestor of ``origin/main`` and this
    turn caused no merge. The preamble MUST NOT instruct the agent to "report
    success" there — a success stamp on a landing this turn did not cause feeds
    the false-success-recurrence sensor the gate exists to starve. This pins the
    invariant against a drift back to the old "Report success" wording.
    """
    # Collapse whitespace so the assertions don't hinge on line-wrap layout.
    collapsed = " ".join(_build_remediation_prompt().lower().split())
    # The branch must direct a partial no-op stand-down, not a success claim.
    assert "report `partial`" in collapsed
    assert "never success" in collapsed
    # The old defect wording must be gone: no "report success" for the landed
    # branch. (The deferral-prohibition paragraph never said "report success".)
    assert "report success" not in collapsed


def test_remediation_prompt_forbids_awaiting_push_without_fetch():
    """`awaiting tend push` is forbidden unless fetch + is-ancestor ran."""
    prompt = _build_remediation_prompt()
    lowered = prompt.lower()
    assert "awaiting tend push" in lowered
    # The prohibition wording must tie the deferral verdict to a fetch having run.
    assert "may not write" in lowered
    assert "ran this turn" in lowered


def test_remediation_prompt_inline_reply_recorded_to_manifest_not_top_level():
    """The Tools-available guidance must steer inline-thread terminal replies
    into the deterministic manifest the loop delivers, not a top-level
    ``gh pr comment`` (which never attaches to the thread).

    On PR #2819 the loop answered an inline review comment with top-level PR
    issue comments, which never attach to the thread, so the comment stayed
    flagged as unreplied. The #2817 fix makes ``run()`` the single trusted
    deliverer: the task RECORDS each terminal reply into ``thread-replies.jsonl``
    and ``run()`` delivers it to the thread via a trusted
    ``reply_to_review_comment`` host call. The reply endpoint is action-gate
    allowed since #2819, but the model must still not post it itself (that would
    double-post the thread). The composed prompt must therefore point inline
    replies at that manifest, keep #2819's warning that a top-level
    ``gh pr comment`` does NOT attach to the thread, and reserve ``gh pr comment``
    for general status and the overflow acknowledgement roll-up.
    """
    prompt = _build_remediation_prompt()
    # Inline replies are recorded into the deterministic manifest the loop
    # delivers — not posted by the task itself (which would double-post).
    assert "thread-replies.jsonl" in prompt
    assert "double-post the thread" in prompt
    # #2819's preserved insight: a top-level comment never attaches to a thread.
    assert "does NOT attach to a thread" in prompt
    # ``gh pr comment`` is reserved for general status / the overflow roll-up.
    assert "overflow-finding acknowledgement roll-up" in prompt


def test_remediation_prompt_delivery_sentence_direct_push():
    """The guidance tells the agent to push its own committed fix to the PR branch.

    Owned-PR self-delivery is unconditional (proposal 093): the commit-delivery
    clause names a direct push, drops the "handled for you" / "delivered
    automatically" framing, and forbids the dangerous verbs. The reply-manifest
    contract and the no-open/merge/ready clause are unchanged — reply delivery
    stays loop-owned because a reply POST is not idempotent.
    """
    prompt = _build_remediation_prompt()
    assert "pushing it to this PR branch" in prompt
    assert "git push origin HEAD:" in prompt
    assert "delivery of your committed fix is handled for you" not in prompt
    assert "delivered to the PR automatically" not in prompt
    # The dangerous verbs are explicitly forbidden.
    assert "force-push" in prompt
    assert "gh pr merge" in prompt
    # The reply-manifest contract and the no-open/merge/ready clause are
    # untouched (reply delivery stays loop-owned).
    assert "record each inline review-thread" in prompt
    assert "thread-replies.jsonl" in prompt
    assert "You may not open, merge, or ready a" in prompt


def test_remediation_prompt_ci_failure_section_placed_after_problem():
    """A non-empty ``ci_failure_section`` renders directly after ``## Problem``
    and before ``## Original Evolution Plan`` (proposal 102), carrying the
    concrete failing line verbatim so it sits beside the failing-check name."""
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    section = (
        "## CI Failure\n- quality:\n"
        "  - FAILED tests/test_widget.py::test_frobnicate - AssertionError\n"
    )
    prompt = _compose_pr_remediation_prompt(
        pr_number=42,
        pr_title="Fix CI",
        plan=None,
        plan_id="ep42",
        pr_body="(body)",
        pr_url="",
        problems=["Head check `quality` is failing before any main-merge."],
        ci_failure_section=section,
    )
    problem_idx = prompt.find("## Problem")
    ci_idx = prompt.find("## CI Failure")
    plan_idx = prompt.find("## Original Evolution Plan")
    assert problem_idx != -1
    assert ci_idx != -1
    assert plan_idx != -1
    # Ordering: the excerpt sits between the problem block and the plan block.
    assert problem_idx < ci_idx
    assert ci_idx < plan_idx
    # The concrete failing line is carried into the prompt verbatim.
    assert "FAILED tests/test_widget.py::test_frobnicate" in prompt


def test_remediation_prompt_ci_failure_section_default_is_byte_identical():
    """The default (empty) ``ci_failure_section`` appends nothing: the composed
    prompt is byte-for-byte identical to an explicit ``""`` and carries no
    ``## CI Failure`` heading (proposal 102 flag-off byte-identical guard)."""
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    default = _compose_pr_remediation_prompt(
        pr_number=42,
        pr_title="Fix CI",
        plan=None,
        plan_id="ep42",
        pr_body="(body)",
        pr_url="",
        problems=["Head check `quality` is failing before any main-merge."],
    )
    explicit_empty = _compose_pr_remediation_prompt(
        pr_number=42,
        pr_title="Fix CI",
        plan=None,
        plan_id="ep42",
        pr_body="(body)",
        pr_url="",
        problems=["Head check `quality` is failing before any main-merge."],
        ci_failure_section="",
    )
    assert default == explicit_empty
    assert "## CI Failure" not in default


def test_remediation_prompt_size_guard_section_carries_cure_hint():
    """A ``ci_failure_section`` naming the source-size guard carries
    the sibling-extraction cure line immediately after the section and before
    ``## Original Evolution Plan``.

    The red that strands a PR on this guard is self-clearable in a single lap,
    but the cure is counter-intuitive (move the addition into a concern-named
    sibling) and the obvious fix — raising the budget — is the one thing the guard forbids. The
    hint puts the cure where the lap already reads the symptom.
    """
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt
    from maestro.prompts.remediation import size_pin_cure_hint

    section = (
        "## CI Failure\n- quality:\n"
        "  - FAILED tests/test_source_file_size.py::"
        "test_source_file_under_split_budget - AssertionError\n"
    )
    prompt = _compose_pr_remediation_prompt(
        pr_number=42,
        pr_title="Fix CI",
        plan=None,
        plan_id="ep42",
        pr_body="(body)",
        pr_url="",
        problems=["Head check `quality` is failing before any main-merge."],
        ci_failure_section=section,
    )
    hint = size_pin_cure_hint(section)
    assert hint in prompt
    ci_idx = prompt.find("## CI Failure")
    hint_idx = prompt.find(hint)
    plan_idx = prompt.find("## Original Evolution Plan")
    assert ci_idx != -1
    assert plan_idx != -1
    # The cure sits directly under the error lines that triggered it, and still
    # ahead of the plan block.
    assert ci_idx < hint_idx
    assert hint_idx < plan_idx


def test_remediation_prompt_other_ci_failure_is_byte_identical():
    """A ``ci_failure_section`` naming any *other* failing gate composes exactly
    the prompt it composed before the cure hint existed: section appended, nothing
    else. Reconstructed here from the section-less prompt plus the section, so the
    assertion is against today's shape rather than a snapshot."""
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    section = (
        "## CI Failure\n- quality:\n"
        "  - FAILED tests/test_widget.py::test_frobnicate - AssertionError\n"
    )
    kwargs = {
        "pr_number": 42,
        "pr_title": "Fix CI",
        "plan": None,
        "plan_id": "ep42",
        "pr_body": "(body)",
        "pr_url": "",
        "problems": ["Head check `quality` is failing before any main-merge."],
    }
    with_section = _compose_pr_remediation_prompt(**kwargs, ci_failure_section=section)
    without = _compose_pr_remediation_prompt(**kwargs)
    # The only difference is the section itself, joined exactly as before: no
    # cure line, no extra separator.
    assert with_section == without.replace(
        "## Original Evolution Plan",
        f"{section}\n## Original Evolution Plan",
        1,
    )
    assert "SPLIT_BUDGET" not in with_section


def test_remediation_preamble_authorizes_chosen_stand_down():
    """The preamble must name the externally-blocked re-lap as avoidance and
    authorize a CHOSEN ``[stood-down]`` ``partial``.

    This pins the subtractive behavioral instruction that supersedes the
    re-lap treadmill: a git-unlanded PR whose *only* blocker is external
    (``AWAITING_APPROVAL``) with no actionable review thread or commit to add
    must be declined, not re-touched. ``AWAITING_PUSH`` is owned (proposal 098) —
    named only in the owned-push instruction, not this external clause. The
    clause sits in the non-zero-exit branch — the cure is genuinely unlanded, yet
    there is no remediation work for the agent to do.
    """
    preamble = MERGE_TRUTH_FIRST_PREAMBLE
    # The decision header naming the move.
    assert "CHOSEN STAND-DOWN" in preamble
    # The verdict token the stand-down gate keys on.
    assert "[stood-down]" in preamble
    # The external blocker that justifies the chosen stand-down.
    assert "AWAITING_APPROVAL" in preamble
    # AWAITING_PUSH is owned — named once, in the owned-push instruction, never in
    # the external chosen-stand-down clause.
    assert preamble.count("AWAITING_PUSH") == 1
    assert "is OWNED the same way" in preamble
    # The behavioral naming: re-admission is avoidance, not tending.
    lowered = preamble.lower()
    assert "avoidance" in lowered
    assert "half-alive" in lowered
    # It must still forbid a success stamp — nothing landed.
    assert "never `success`" in preamble


def test_remediation_prompt_includes_chosen_stand_down_clause():
    """The assembled remediation prompt must carry the chosen stand-down clause.

    The builder prepends ``MERGE_TRUTH_FIRST_PREAMBLE`` unchanged, so every
    new directive token reaches the agent at action-zero. ``AWAITING_PUSH`` is
    owned (proposal 098) — named once, as the owned-push act.
    """
    prompt = _build_remediation_prompt()
    assert "CHOSEN STAND-DOWN" in prompt
    assert "[stood-down]" in prompt
    assert "AWAITING_APPROVAL" in prompt
    # AWAITING_PUSH survives only as the owned-push terminal act.
    assert prompt.count("AWAITING_PUSH") == 1
    assert "is OWNED the same way" in prompt
    assert "or `AWAITING_PUSH` (Tend will deliver the push)" not in prompt
    # The preamble is prepended verbatim — accessor and constant agree, and the
    # full preamble string is a substring of the assembled prompt.
    assert merge_truth_first_preamble() == MERGE_TRUTH_FIRST_PREAMBLE
    assert MERGE_TRUTH_FIRST_PREAMBLE in prompt


def test_remediation_prompt_includes_push_first_clause():
    """The assembled remediation prompt must carry the PUSH-FIRST clause.

    The builder prepends ``MERGE_TRUTH_FIRST_PREAMBLE`` verbatim, so the
    push-first directive — push a reconciliation-only resolution before running
    the local gates on a green PR — reaches the agent at action-zero. Re-assert
    the single-mention ``AWAITING_PUSH`` guard so the new clause did not disturb
    it (proposal 098 pins it at exactly one, in the owned-push instruction).
    """
    prompt = _build_remediation_prompt()
    assert "PUSH-FIRST" in prompt
    assert "push the resolution FIRST" in prompt
    # The new clause introduced no second AWAITING_PUSH mention.
    assert prompt.count("AWAITING_PUSH") == 1


def test_remediation_preamble_follows_header_before_problem():
    """The preamble must sit after the parseable header but before the work.

    The ``Remediate PR #<n>:`` header MUST stay the literal first line — the
    dedup prefix and every dequeue-gate ``re.match`` anchor on it — so the
    preamble cannot lead the string. It must still precede the ``## Problem``
    section, making it the first *instruction* the agent acts on.
    """
    prompt = _build_remediation_prompt()
    # Header parsing the gates depend on must survive the preamble insertion.
    assert prompt.startswith("Remediate PR #2662: Fix conflicts")
    header_idx = prompt.find("Remediate PR #2662")
    preamble_idx = prompt.find("MERGE-TRUTH-FIRST")
    problem_idx = prompt.find("## Problem")
    assert header_idx != -1
    assert preamble_idx != -1
    assert problem_idx != -1
    assert header_idx < preamble_idx < problem_idx


def test_remediation_prompt_surfaces_gh_pr_tools():
    """The guidance must surface the PR tools the task gate now permits.

    ``gh pr comment`` (proposal 064) and the read-only ``gh pr view`` /
    ``gh pr checks`` verbs pass the task-execution gate, but the prose has to
    tell the agent they exist. The boundary the gate enforces (no open / merge
    / ready) is restated, and the merge verdict stays anchored to the git
    merge-truth check above — never a ``gh``-reported state.
    """
    lowered = _build_remediation_prompt().lower()
    assert "gh pr comment" in lowered
    assert "gh pr view" in lowered
    assert "gh pr checks" in lowered
    assert "may not open, merge, or ready" in lowered
    assert "never from a `gh`-reported state" in lowered


# =====================================================================
# format_acceptance_criteria_block — shared renderer
# =====================================================================

# Two fixed criteria; the "binding"/"plain"/"remediation" goldens below are
# captured verbatim from the pre-refactor call sites so the dedup is proven
# byte-for-byte behaviour-preserving.
_CRITERIA = ["First invariant holds.", "Second invariant holds."]


def test_acceptance_block_binding_golden() -> None:
    """The "binding" style reproduces the self-review block verbatim."""
    assert format_acceptance_criteria_block(_CRITERIA, style="binding") == (
        "\n## Acceptance Criteria (binding)\n"
        "For each item below, you MUST cite (a) the diff hunk implementing"
        " it and (b) the test asserting it. Any criterion missing either"
        " citation is a blocking defect — fix it before responding LGTM.\n"
        "1. First invariant holds.\n"
        "2. Second invariant holds.\n"
    )


def test_acceptance_block_writing_golden() -> None:
    """The "writing" style shares the header but uses writing-phase contract."""
    assert format_acceptance_criteria_block(_CRITERIA, style="writing") == (
        "\n## Acceptance Criteria (binding)\n"
        "Implement each criterion below and add a test that would fail if it"
        " were violated; these are the binding success contract this change"
        " will be judged against.\n"
        "1. First invariant holds.\n"
        "2. Second invariant holds.\n"
    )


def test_acceptance_block_plain_golden() -> None:
    """The "plain" style reproduces the PR-body section verbatim."""
    assert format_acceptance_criteria_block(_CRITERIA, style="plain") == (
        "\n\n### Acceptance Criteria\n"
        "- First invariant holds.\n"
        "- Second invariant holds."
    )


def test_acceptance_block_remediation_golden() -> None:
    """The "remediation" style reproduces the indented sub-list verbatim."""
    assert format_acceptance_criteria_block(
        _CRITERIA, style="remediation",
    ) == (
        "- Acceptance criteria:\n"
        "  - First invariant holds.\n"
        "  - Second invariant holds."
    )


@pytest.mark.parametrize(
    "style", ["binding", "writing", "plain", "remediation"],
)
def test_acceptance_block_empty_returns_empty(
    style: AcceptanceCriteriaStyle,
) -> None:
    """Every style returns "" for empty criteria so callers need no guard."""
    assert format_acceptance_criteria_block([], style=style) == ""


@pytest.mark.parametrize(
    "style", ["binding", "writing", "plain", "remediation"],
)
def test_acceptance_block_renders_at_cap(
    style: AcceptanceCriteriaStyle,
) -> None:
    """Every style renders a list exactly at the documented 1-5 cap."""
    at_cap = [
        f"Criterion {i} holds." for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
    ]
    rendered = format_acceptance_criteria_block(at_cap, style=style)
    for criterion in at_cap:
        assert criterion in rendered


@pytest.mark.parametrize(
    "style", ["binding", "writing", "plain", "remediation"],
)
def test_acceptance_block_rejects_over_cap(
    style: AcceptanceCriteriaStyle,
) -> None:
    """The renderer is the choke: an over-cap list never reaches a prompt.

    A legacy row the loader admitted intact would otherwise bloat all four
    renderings with an unbounded contract. Failing here surfaces the offender
    at the moment it would spend context, so a human re-scopes it to <=5
    before the plan is implemented.
    """
    over_cap = [
        f"Criterion {i} holds."
        for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    ]
    with pytest.raises(ValueError, match="acceptance_criteria"):
        format_acceptance_criteria_block(over_cap, style=style)


def _remediation_plan_with_criteria(criteria: list[str]) -> Any:
    """A minimal ``EvolutionPlan`` carrying *criteria*, for prompt composition."""
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id="ep-criteria",
        title="Legacy plan",
        rationale="Written before the count bound landed.",
        category="capability",
        priority="normal",
        proposed_changes=["Touch one file."],
        files_affected=["maestro/widget.py"],
        expected_impact="none",
        acceptance_criteria=criteria,
    )


def _compose_with_plan(criteria: list[str]) -> str:
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    return _compose_pr_remediation_prompt(
        pr_number=7,
        pr_title="Legacy PR",
        plan=_remediation_plan_with_criteria(criteria),
        plan_id="ep-criteria",
        pr_body="(body)",
        pr_url="",
        problems=["The branch is behind main."],
    )


def test_remediation_prompt_renders_at_cap_criteria() -> None:
    """An at-cap plan renders its criteria into the remediation prompt."""
    at_cap = [
        f"Criterion {i} holds." for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)
    ]
    prompt = _compose_with_plan(at_cap)
    for criterion in at_cap:
        assert criterion in prompt


def test_remediation_prompt_omits_over_cap_criteria_without_raising() -> None:
    """A legacy over-cap plan yields an honest omission line, not a crash.

    The renderer's refusal is correct — an unbounded contract must not reach a
    prompt — but this prompt is composed for a PR that already exists, so there
    is no plan to dispose. Naming the omission keeps the writer from believing
    it satisfied criteria it never saw.
    """
    over_cap = [
        f"Criterion {i} holds."
        for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)
    ]
    prompt = _compose_with_plan(over_cap)
    assert "Acceptance criteria: omitted" in prompt
    assert str(len(over_cap)) in prompt
    # No criterion body leaks through the omission line.
    for criterion in over_cap:
        assert criterion not in prompt


# --- Objective prompt selector ---


def test_select_objective_prompt_returns_objective() -> None:
    assert select_objective_prompt() == OBJECTIVE_PROMPT
