"""Tests for the record-time actuators in ``outcome_grounding``.

These cover the three record-time actuators
(``bind_mergeable_flip_to_merge_request``, ``bind_ci_failing_verdict_to_red_read``,
``bind_costume_discharge_to_partial``) and the retained
``extract_self_committed_shas`` helper, plus their wiring into
``MaestroAgent.run``'s outcome-assessment path — where the remediation-PR landing
verdict itself is authored by the §5d mechanical authority
(``outcome_authority.decide_outcome``). All git/host I/O is mocked per Testing
Principles (no real subprocess, no real network).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.audit_checks_phantom import (
    _ON_BRANCH_HANDOFF_MARKERS,
)
from maestro.narration_cues import PROTECTED_ADMISSION_PHRASES
from maestro.narration_cues import RESOLUTION_RE as _NARRATION_RESOLUTION_RE
from maestro.outcome_grounding import (
    _FRAME_DISCHARGE_MISMATCH_NOTE,
    _MERGEABLE_FLIP_BLOCKERS,
    _OPEN_THE_RED_GATE_DIRECTIVE,
    _RED_UNREAD_NOTE,
    JsonMergeRequestServicedStore,
    MergeableFlipContext,
    _scrub_resolution_keywords,
    bind_ci_failing_verdict_to_red_read,
    bind_costume_discharge_to_partial,
    bind_mergeable_flip_to_merge_request,
    extract_self_committed_shas,
)
from maestro.pr_facts import MergeTruth, PRReadiness
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_THREADS_UNRESOLVED,
    classify_landing_attribution,
    classify_noop,
)
from maestro.sdk import ToolCall
from tests.conftest import MockQueryResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from maestro.agent import MaestroAgent


# ── bind_ci_failing_verdict_to_red_read pure-function branches ───────────

_RED_READ_FIRE_KWARGS = {
    "blocker_ci_failing": True,
    "oracle_surfaced_failure": True,
    "command_record_nonempty": True,
}


def test_bind_ci_failing_neutralizes_and_stamps_on_all_four() -> None:
    """Criterion 2: all four conditions hold → resolution keywords scrubbed from
    summary + findings, outcome forced ``partial``, a ``[red-unread]`` note
    appended to the uncapped findings, and the open-the-red-gate directive
    returned."""
    # Free-floating keywords ("Resolved it", "Fixed the check") — deliberately
    # NOT a PROTECTED_ADMISSION_PHRASE, so the scrub neutralizes them in full.
    # (A protected phrase like "resolved on branch" survives by design, #2970 —
    # covered by ``test_bind_ci_failing_preserves_admission_phrase``.)
    summary, findings, outcome, directive = bind_ci_failing_verdict_to_red_read(
        "Resolved it; delivered to the PR.",
        "Fixed the check on the branch.",
        "success",
        **_RED_READ_FIRE_KWARGS,
    )
    assert outcome == "partial"
    assert directive == _OPEN_THE_RED_GATE_DIRECTIVE
    # The [red-unread] evidence lands in findings, never the capped summary.
    assert findings is not None
    assert _RED_UNREAD_NOTE in findings
    assert _RED_UNREAD_NOTE not in summary
    # Both prose fields have their agent resolution keywords neutralized.
    assert _NARRATION_RESOLUTION_RE.search(summary) is None
    assert _NARRATION_RESOLUTION_RE.search(findings) is None


def test_bind_ci_failing_fires_with_none_and_keyword_free_findings() -> None:
    """Criterion 2: the fire path handles ``key_findings is None`` (the note alone
    becomes the findings) and keyword-free findings (kept, then the note appended)
    — the summary keyword still drives the completion-narration detection."""
    summary_none, findings_none, outcome_none, _ = (
        bind_ci_failing_verdict_to_red_read(
            "Resolved it on the branch.", None, "partial",
            **_RED_READ_FIRE_KWARGS,
        )
    )
    assert outcome_none == "partial"
    assert findings_none == _RED_UNREAD_NOTE
    assert _RED_UNREAD_NOTE not in summary_none

    _s, findings_free, _o, _d = bind_ci_failing_verdict_to_red_read(
        "Resolved it on the branch.", "no keyword here", "partial",
        **_RED_READ_FIRE_KWARGS,
    )
    assert findings_free is not None
    assert findings_free.startswith("no keyword here")
    assert _RED_UNREAD_NOTE in findings_free


def test_bind_ci_failing_protects_markers_and_loop_notes() -> None:
    """Criterion 3: the bind never scrubs a loop-authored merge-truth note or any
    ``Marker``-protected substring, even when they contain a resolution keyword —
    only the agent's own keyword is neutralized."""
    note = (
        "Resolved on branch, awaiting tend push — PR #2811 not yet landed "
        "per merge-truth."
    )
    summary = f"{note} {Marker.LANDED}: I fixed it myself."
    out_summary, out_findings, outcome, directive = (
        bind_ci_failing_verdict_to_red_read(
            summary, None, "partial",
            protected_note=note, **_RED_READ_FIRE_KWARGS,
        )
    )
    assert outcome == "partial"
    assert directive == _OPEN_THE_RED_GATE_DIRECTIVE
    # The Marker (which contains "landed") and the honest note survive verbatim.
    assert str(Marker.LANDED) in out_summary
    assert note in out_summary
    # The agent's own "fixed" is neutralized.
    assert "fixed" not in out_summary.lower()
    # The [red-unread] evidence lands in findings.
    assert out_findings is not None
    assert _RED_UNREAD_NOTE in out_findings


# ── #2970: admission phrases survive the scrub, sensors stay sighted ──────


def test_scrub_preserves_every_admission_phrase_byte_for_byte() -> None:
    """#2970: each ``PROTECTED_ADMISSION_PHRASES`` entry survives the scrub verbatim.

    A downstream sensor admits a record on a case-insensitive prose substring that
    itself carries a resolution keyword; it reads the scrubbed summary, so the
    phrase must pass through untouched even though its keyword would otherwise be
    rewritten to the neutral token.
    """
    for phrase in PROTECTED_ADMISSION_PHRASES:
        text = f"lap note — {phrase} — end."
        assert _scrub_resolution_keywords(text) == text


def test_scrub_still_rewrites_a_free_floating_keyword() -> None:
    """The protection is surgical: a resolution keyword outside any admission
    phrase is still neutralized to ``seen``, and a phrase co-located with a free
    keyword keeps the phrase while losing the free keyword."""
    assert _scrub_resolution_keywords("resolved the ticket") == "seen the ticket"
    # "resolved on branch" is protected; the trailing free "merged" is not.
    scrubbed = _scrub_resolution_keywords("resolved on branch, then merged it")
    assert "resolved on branch" in scrubbed
    assert "merged" not in scrubbed
    assert "seen it" in scrubbed


def test_scrub_does_not_clip_a_phrase_that_is_a_substring_of_another() -> None:
    """Longest-first protection keeps ``was already merged`` intact even though the
    shorter ``already merged`` is a protected substring of it — the longer phrase
    is folded into its sentinel first, so the shorter phrase's later pass finds
    nothing left to clip."""
    text = "it was already merged upstream"
    assert _scrub_resolution_keywords(text) == text


def test_scrubbed_on_branch_handoff_still_admits_under_rule_14() -> None:
    """#2970 core: the record-time scrub no longer blinds rule 14.

    A ``partial`` on-branch handoff summary carrying an own committed SHA that
    never reached ``origin/main`` still admits under
    ``_ON_BRANCH_HANDOFF_MARKERS`` after the scrub, so it stays flaggable past its
    grace window instead of silently dropping out."""
    scrubbed = _scrub_resolution_keywords(
        "Resolved on branch for PR #2638; committed ac48a769",
    )
    assert "resolved on branch" in scrubbed.lower()
    assert any(marker in scrubbed.lower() for marker in _ON_BRANCH_HANDOFF_MARKERS)


def test_scrubbed_already_resolved_still_reads_as_noop() -> None:
    """#2970: the repetition gate's text axis stays sighted — a scrubbed
    ``already resolved`` no-op still returns True from ``classify_noop``."""
    scrubbed = _scrub_resolution_keywords(
        "No remediation needed; already resolved upstream",
    )
    assert classify_noop(scrubbed) is True


def test_scrubbed_confirmed_survives_but_caused_is_neutralized() -> None:
    """#2970: the scrub keeps a CONFIRMED landing readable while deliberately
    neutralizing a CAUSED one.

    ``classify_landing_attribution`` is read on scrubbed text only in the
    red-read-bind costume slice (the lap addressed nothing). A CONFIRMED marker
    must survive so rule 14 still skips another party's land; a CAUSED marker must
    NOT — protecting it would let the costume read CAUSED once §5d flips it to
    ``success``, inflating the confirmed-isn't-caused over-claim
    ``measure_confirmed_landing_rate`` exists to expose (r1). The CONFIRMED half
    relies solely on the keyword-bearing ``git shows merged`` surviving — no
    keyword-free fallback marker carries it (r2)."""
    confirmed = _scrub_resolution_keywords("Git shows merged; nothing owed here")
    assert "git shows merged" in confirmed.lower()
    assert classify_landing_attribution(
        {"outcome": "success", "result_summary": confirmed},
    ) == "CONFIRMED"
    # A CAUSED phrase is deliberately scrubbed → the costume reads NA, not CAUSED.
    caused = _scrub_resolution_keywords("I merged origin/main into this branch")
    assert "merged origin/main" not in caused.lower()
    assert classify_landing_attribution(
        {"outcome": "success", "result_summary": caused},
    ) == "NA"


def test_bind_ci_failing_preserves_admission_phrase() -> None:
    """#2970: when the bind fires on a phrase-bearing record it downgrades to
    ``partial`` and appends the ``[red-unread]`` note (honest), yet the on-branch
    handoff phrase survives so rule 14 can still see the unconfirmed landing."""
    summary, findings, outcome, directive = bind_ci_failing_verdict_to_red_read(
        "Resolved on branch for PR #2638; committed ac48a769",
        None,
        "success",
        **_RED_READ_FIRE_KWARGS,
    )
    assert outcome == "partial"
    assert directive == _OPEN_THE_RED_GATE_DIRECTIVE
    assert "resolved on branch" in summary.lower()
    assert any(marker in summary.lower() for marker in _ON_BRANCH_HANDOFF_MARKERS)
    assert findings == _RED_UNREAD_NOTE


def test_bind_ci_failing_noop_when_blocker_not_ci_failing() -> None:
    """Criterion 2 guard: a non-CI_FAILING blocker → inputs unchanged."""
    args = ("Resolved it.", "Fixed the check.", "partial")
    assert bind_ci_failing_verdict_to_red_read(
        *args, blocker_ci_failing=False, oracle_surfaced_failure=True,
        command_record_nonempty=True,
    ) == (*args, None)


def test_bind_ci_failing_noop_when_command_record_empty() -> None:
    """Criterion 2 guard: an empty command record → inputs unchanged (no evidence
    the red was ignored)."""
    args = ("Resolved it.", "Fixed the check.", "partial")
    assert bind_ci_failing_verdict_to_red_read(
        *args, blocker_ci_failing=True, oracle_surfaced_failure=True,
        command_record_nonempty=False,
    ) == (*args, None)


def test_bind_ci_failing_noop_without_completion_narration() -> None:
    """A record that narrates no completion (no resolution keyword) is left
    untouched even with a CI_FAILING blocker and no red-read."""
    args = ("Investigated the flake; still red.", None, "partial")
    assert bind_ci_failing_verdict_to_red_read(
        *args, **_RED_READ_FIRE_KWARGS,
    ) == (*args, None)


def test_bind_ci_failing_noop_on_non_completion_outcome() -> None:
    """A ``failed`` outcome never narrates completion, so the bind stands aside even
    when a resolution keyword is present."""
    args = ("Resolved it on the branch.", None, "failed")
    assert bind_ci_failing_verdict_to_red_read(
        *args, **_RED_READ_FIRE_KWARGS,
    ) == (*args, None)


# ── fire-signal guards (surfaced-but-unaddressed red) ────────────────────


def test_bind_ci_failing_noop_when_addressed() -> None:
    """A surfaced red the lap committed a fix for → verdict passes through.

    ``addressed_red`` True means the lap edited-and-pushed a fix, so the honest
    signal is that the red was worked, not ignored — no scrub, no directive.
    """
    args = ("Resolved it on the branch.", "Fixed the check.", "success")
    assert bind_ci_failing_verdict_to_red_read(
        *args,
        blocker_ci_failing=True,
        command_record_nonempty=True,
        oracle_surfaced_failure=True,
        addressed_red=True,
    ) == (*args, None)


def test_bind_ci_failing_noop_when_no_surfaced_line() -> None:
    """CI failing but the oracle surfaced no concrete line → stands aside.

    With nothing concrete surfaced, there is no ignored red to penalise, so the
    guard is a no-op.
    """
    args = ("Resolved it on the branch.", "Fixed the check.", "success")
    assert bind_ci_failing_verdict_to_red_read(
        *args,
        blocker_ci_failing=True,
        command_record_nonempty=True,
        oracle_surfaced_failure=False,
        addressed_red=False,
    ) == (*args, None)


# ── bind_costume_discharge_to_partial pure-function branches ─────────────


def test_bind_costume_caps_success_and_appends_note() -> None:
    """A COSTUME success is capped to partial with the mismatch note appended."""
    outcome, findings = bind_costume_discharge_to_partial(
        "found a marker", "success", is_costume=True,
    )
    assert outcome == "partial"
    assert findings is not None
    assert findings.startswith("found a marker")
    assert _FRAME_DISCHARGE_MISMATCH_NOTE in findings


def test_bind_costume_absent_findings_becomes_the_note_alone() -> None:
    """With no prior key_findings, the note stands alone (no leading space)."""
    outcome, findings = bind_costume_discharge_to_partial(
        None, "success", is_costume=True,
    )
    assert outcome == "partial"
    assert findings == _FRAME_DISCHARGE_MISMATCH_NOTE


def test_bind_costume_noop_when_not_costume() -> None:
    """A non-COSTUME verdict leaves outcome and findings byte-for-byte unchanged."""
    assert bind_costume_discharge_to_partial(
        "clean read", "success", is_costume=False,
    ) == ("success", "clean read")


def test_bind_costume_noop_when_outcome_not_success() -> None:
    """A COSTUME whose outcome is already partial/failure is not re-capped."""
    assert bind_costume_discharge_to_partial(
        "notes", "partial", is_costume=True,
    ) == ("partial", "notes")
    assert bind_costume_discharge_to_partial(
        "notes", "failure", is_costume=True,
    ) == ("failure", "notes")


# ── agent.run wiring (criterion 4) ──────────────────────────────────────

_PLAN = json.dumps([{"description": "Step 1"}])


async def _mock_q(*args: object, **kwargs: object) -> MockQueryResult:
    return MockQueryResult(response="Resolved on the branch.")


def _arm(agent: object) -> None:
    """Configure *agent* so a remediation reaches §5d still stamped success.

    The legacy stamp gate (§4c-else) keeps ``success`` when git-subject scan
    reports the PR merged (``_git_pr_merged`` True), and the step output carries
    a resolution keyword so the §5d authority has a landing claim to demote.
    """
    agent.config.enable_session_reuse = False  # type: ignore[attr-defined]
    agent._vcs.has_changes.return_value = True  # type: ignore[attr-defined]
    # git-subject scan says the cure landed (a squash merge), so §4c keeps the
    # success stamp — but ``is_pr_settled.branch_landed`` (strict ancestor) is
    # what the §5d authority reads.
    agent._git_pr_merged = MagicMock(return_value=True)  # type: ignore[attr-defined]


def _mechanical_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The ``mechanical_outcome_authority`` (§5d) activation rows *agent* wrote."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [r for r in rows if r["gate"] == "mechanical_outcome_authority"]


@pytest.mark.asyncio
async def test_run_downgrades_landing_claim_on_unmerged_branch(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A landing-claiming ``success`` whose branch is not an ancestor of
    ``origin/main`` (``is_pr_settled.branch_landed`` False) is recorded
    ``partial`` by the §5d mechanical authority."""
    agent = make_agent()
    _arm(agent)
    # Conftest default: is_pr_settled → OPEN / branch_landed False (unmerged).

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Resolved the issue")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    # Proposal 105: with the prose-keyed pre-downgrade gone, the §5d authority now
    # sees the true ``success`` narration and records a genuine ``diverged`` row —
    # the honest phantom-landing measurement, not the pre-corrected ``narr=partial``
    # the deleted keyword bind used to produce.
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "diverged"
    assert "narr=success" in rows[0]["detail"]
    assert "mech=partial" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_run_preserves_success_when_branch_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When ``is_pr_settled.branch_landed`` confirms the branch reached
    ``origin/main``, the §5d authority records ``success``, so the
    landing-claiming ``success`` is preserved."""
    agent = make_agent()
    _arm(agent)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Resolved the issue")
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "success"


# ── extract_self_committed_shas (#2848, FM4) ─────────────────────────────


def test_extract_self_committed_shas_matches_admitted_cue_shapes() -> None:
    """The dedicated FM4 extractor matches every admitted cue shape — including
    the canonical ``committed as `<sha>``` the shared ``_COMMITTED_SHA_RE`` misses
    — and rejects a cue-less hex word."""
    sha = "5971202d"
    assert extract_self_committed_shas(f"committed as `{sha}`") == {sha}
    assert extract_self_committed_shas(f"committed {sha}") == {sha}
    assert extract_self_committed_shas(f"committed on this branch {sha}") == {sha}
    assert extract_self_committed_shas(f"sha {sha}") == {sha}
    assert extract_self_committed_shas(f"@{sha}") == {sha}
    # Case-insensitive (mirrors rule 14's ``findall(lowered)``) so a capitalised
    # distilled "Committed as ..." still fires in production.
    assert extract_self_committed_shas(f"Committed as `{sha}`") == {sha}


def test_extract_self_committed_shas_rejects_cueless_hex_and_empty() -> None:
    """A cue-less hex word is not a citation; empty/falsy text yields no SHAs."""
    assert extract_self_committed_shas("the build deadbeef was green") == set()
    assert extract_self_committed_shas("") == set()


# ── mergeable-flip merge-request actuator (pure) ─────────────────────────

_FLIP_DIR = Path("/fake/agent")
_FLIP_VERDICT = "Remediate PR #2647: reconciled the branch to a mergeable flip."


def _flip_ctx(
    *,
    pr_number: int = 2647,
    head_sha: str = "abc123head",
    settled: bool = False,
    branch_landed: bool = False,
    blocker: str = MERGE_BLOCK_AWAITING_APPROVAL,
) -> MergeableFlipContext:
    """A ``MergeableFlipContext`` in the fire state, overridable per-branch."""
    return MergeableFlipContext(
        pr_number=pr_number,
        head_sha=head_sha,
        settled=settled,
        branch_landed=branch_landed,
        blocker=blocker,
    )


def _serviced_backend() -> JsonMergeRequestServicedStore:
    """A JSON serviced-marker backend over the shared ``_FLIP_DIR`` sidecar."""
    return JsonMergeRequestServicedStore(_FLIP_DIR)


def _recording_deliver(
    calls: list[MergeableFlipContext], *, returns: bool = True,
) -> Callable[[MergeableFlipContext], bool]:
    """A ``deliver`` double that records every ctx it receives."""
    def _deliver(ctx: MergeableFlipContext) -> bool:
        calls.append(ctx)
        return returns
    return _deliver


def test_mergeable_flip_delivers_named_merge_request(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1: verdict cites #NNNN + clean-but-unlanded + external-only
    mergeable-terminal blocker (AWAITING_APPROVAL) → exactly one named
    merge-request via ``deliver``."""
    calls: list[MergeableFlipContext] = []
    out = bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT,
        _flip_ctx(blocker=MERGE_BLOCK_AWAITING_APPROVAL),
        _recording_deliver(calls),
        backend=_serviced_backend(),
    )
    assert out is True
    assert len(calls) == 1
    # The delivered ctx names the PR (#NNNN) the merge-request recruits on.
    assert calls[0].pr_number == 2647


def test_mergeable_flip_idempotent_per_pr_head(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2: a second invocation for the same (pr_number, head_sha) with a
    delivery already recorded produces zero additional deliveries; a genuinely new
    head is serviced afresh."""
    calls: list[MergeableFlipContext] = []
    deliver = _recording_deliver(calls)
    backend = _serviced_backend()
    ctx = _flip_ctx()
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is True
    # Same (pr, head) — marker recorded → no second delivery.
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is False
    assert len(calls) == 1
    # A new head on the same PR is a fresh (pr, head) key → serviced again.
    ctx2 = _flip_ctx(head_sha="def456head")
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx2, deliver, backend=backend,
    ) is True
    assert len(calls) == 2


def test_mergeable_flip_persists_through_shared_backend(
    mem_fs: dict[str, str],
) -> None:
    """One ``JsonMergeRequestServicedStore`` instance carries a serviced pair
    across two ``bind_`` calls: the second call for the same (pr, head) returns
    False (the recorded marker suppresses the re-knock), while a genuinely new head
    is serviced afresh — the per-head idempotency the two-method backend seam
    preserves without the former single ``file_lock``."""
    calls: list[MergeableFlipContext] = []
    deliver = _recording_deliver(calls)
    backend = JsonMergeRequestServicedStore(_FLIP_DIR)
    ctx = _flip_ctx()
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is True
    # Same instance, same (pr, head) — the recorded marker suppresses a re-knock.
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is False
    # A new head is a fresh key → the shared backend services it.
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, _flip_ctx(head_sha="newhead999"), deliver,
        backend=backend,
    ) is True
    assert len(calls) == 2


def test_mergeable_flip_owned_blocker_delivers_nothing(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3a: an owned blocker (CONFLICTING/DIRTY/THREADS) is mine to clear,
    so the actuator stands aside and delivers nothing."""
    calls: list[MergeableFlipContext] = []
    for blocker in (
        MERGE_BLOCK_CONFLICTING, "DIRTY", MERGE_BLOCK_THREADS_UNRESOLVED,
    ):
        out = bind_mergeable_flip_to_merge_request(
            _FLIP_VERDICT, _flip_ctx(blocker=blocker), _recording_deliver(calls),
            backend=_serviced_backend(),
        )
        assert out is False
    assert calls == []


def test_mergeable_flip_already_landed_or_settled_delivers_nothing(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3b: a PR the merge truth shows already landed (or otherwise
    settled, e.g. closed-unmerged) has nothing left to recruit — no delivery."""
    calls: list[MergeableFlipContext] = []
    deliver = _recording_deliver(calls)
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, _flip_ctx(branch_landed=True), deliver,
        backend=_serviced_backend(),
    ) is False
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, _flip_ctx(settled=True), deliver,
        backend=_serviced_backend(),
    ) is False
    assert calls == []


def test_mergeable_flip_non_terminal_external_blocker_delivers_nothing(
    mem_fs: dict[str, str],
) -> None:
    """A branch that is external-owned but NOT mergeable-terminal (behind main / a
    CI state) is not yet mergeable, so a merge-request would be premature — the
    actuator narrows to AWAITING_APPROVAL only."""
    calls: list[MergeableFlipContext] = []
    for blocker in (MERGE_BLOCK_BEHIND_MAIN, "CI_FAILING", "CI_PENDING", ""):
        out = bind_mergeable_flip_to_merge_request(
            _FLIP_VERDICT, _flip_ctx(blocker=blocker), _recording_deliver(calls),
            backend=_serviced_backend(),
        )
        assert out is False
    assert calls == []


@pytest.mark.parametrize("own_push", [False, True])
def test_mergeable_flip_own_push_flag_leaves_behaviour_unchanged(
    own_push: bool,
    mem_fs: dict[str, str],
) -> None:
    """Proposal 098: threading ``own_push`` into the single-source ownership
    classifier leaves the actuator's externally-observable behaviour identical
    under both flag values. ``AWAITING_PUSH`` never delivers — owned when the flag
    is on (the agent pushes its own resolution), otherwise external but not a
    mergeable-flip terminal — and ``AWAITING_APPROVAL`` still delivers under both,
    since the flag flips only ``AWAITING_PUSH``'s ownership."""
    push_calls: list[MergeableFlipContext] = []
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT,
        _flip_ctx(blocker=MERGE_BLOCK_AWAITING_PUSH),
        _recording_deliver(push_calls),
        backend=_serviced_backend(),
        own_push=own_push,
    ) is False
    assert push_calls == []

    approval_calls: list[MergeableFlipContext] = []
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT,
        _flip_ctx(blocker=MERGE_BLOCK_AWAITING_APPROVAL),
        _recording_deliver(approval_calls),
        backend=_serviced_backend(),
        own_push=own_push,
    ) is True
    assert len(approval_calls) == 1


def _readiness_with_flags(
    *, conflicting: bool, behind: bool, awaiting_approval: bool,
) -> PRReadiness:
    """A ``PRReadiness`` with the three merge-block flags set explicitly — the
    only inputs :meth:`PRReadiness.merge_block_reason` reads."""
    return PRReadiness(
        pr_number=2647,
        branch_landed=False,
        branch_behind=behind,
        branch_conflicting=conflicting,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
    )


def test_mergeable_flip_blockers_are_all_caller_reachable(
    mem_fs: dict[str, str],
) -> None:
    """Reachability regression (PR #3083 comment 3599702832): every blocker the
    actuator accepts must be one the wired caller can actually feed it. The caller
    (agent.py §5c-iv-b) derives the blocker from
    ``PRReadiness.merge_block_reason()``; enumerating that method over every
    branch-flag combination yields the full reachable set. It excludes
    ``AWAITING_PUSH`` (a local-only-commits state ``classify_merge_block`` — the
    oracle behind the readiness fact — never emits), so an actuator that accepted
    ``AWAITING_PUSH`` carried a dead branch. Assert the accepted set is exactly the
    reachable, human-recruitable blocker, and that a ctx carrying the unreachable
    ``AWAITING_PUSH`` is declined."""
    reachable = {
        _readiness_with_flags(
            conflicting=conflicting, behind=behind, awaiting_approval=approval,
        ).merge_block_reason()
        for conflicting in (False, True)
        for behind in (False, True)
        for approval in (False, True)
    }
    assert MERGE_BLOCK_AWAITING_PUSH not in reachable
    assert frozenset({MERGE_BLOCK_AWAITING_APPROVAL}) == _MERGEABLE_FLIP_BLOCKERS
    # No accepted blocker is unreachable through the wiring.
    assert reachable >= _MERGEABLE_FLIP_BLOCKERS
    # The actuator declines the unreachable code even if a ctx is hand-built.
    calls: list[MergeableFlipContext] = []
    out = bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT,
        _flip_ctx(blocker=MERGE_BLOCK_AWAITING_PUSH),
        _recording_deliver(calls),
        backend=_serviced_backend(),
    )
    assert out is False
    assert calls == []


def test_mergeable_flip_requires_pr_citation(
    mem_fs: dict[str, str],
) -> None:
    """A verdict that does not cite this PR's #NNNN never fires (the actuator only
    acts on a verdict genuinely about the PR)."""
    calls: list[MergeableFlipContext] = []
    out = bind_mergeable_flip_to_merge_request(
        "reconciled a branch, no number here", _flip_ctx(),
        _recording_deliver(calls), backend=_serviced_backend(),
    )
    assert out is False
    assert calls == []


def test_mergeable_flip_tolerates_corrupt_serviced_ledger(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt serviced-marker ledger degrades to an empty ledger (no crash),
    so the actuator still fires — the marker is an idempotency convenience, not a
    correctness gate. Exercises the JSON adapter's corrupt-tolerant read."""
    marker_path = "/fake/agent/mergeable_merge_requests.json"
    # Non-dict root (a JSON list) → treated as an empty ledger.
    mem_fs[marker_path] = "[]"
    calls: list[MergeableFlipContext] = []
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, _flip_ctx(), _recording_deliver(calls),
        backend=_serviced_backend(),
    ) is True
    assert len(calls) == 1
    # A non-digit key and a non-list value are both skipped as corruption.
    mem_fs[marker_path] = json.dumps({"notpr": ["x"], "2647": "notalist"})
    calls2: list[MergeableFlipContext] = []
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, _flip_ctx(head_sha="freshhead"),
        _recording_deliver(calls2), backend=_serviced_backend(),
    ) is True
    assert len(calls2) == 1


def test_mergeable_flip_suppressed_delivery_not_marked(
    mem_fs: dict[str, str],
) -> None:
    """When ``deliver`` returns False (its own idempotency backstop suppressed the
    knock), no serviced marker is recorded — so a suppressed delivery cannot mask
    a later genuine knock on the same head."""
    calls: list[MergeableFlipContext] = []
    deliver = _recording_deliver(calls, returns=False)
    backend = _serviced_backend()
    ctx = _flip_ctx()
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is False
    # Not marked → the same (pr, head) is re-evaluated and deliver runs again.
    assert bind_mergeable_flip_to_merge_request(
        _FLIP_VERDICT, ctx, deliver, backend=backend,
    ) is False
    assert len(calls) == 2


# ── agent.run §5c-iv-b mergeable-flip actuator wiring ────────────────────


def _flip_readiness_awaiting_approval() -> PRReadiness:
    """A ``PRReadiness`` whose sole blocker is external approval (AWAITING_APPROVAL)
    — the mergeable-flip terminal the wired actuator fires on."""
    return PRReadiness(
        pr_number=2647,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state="GREEN",
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=True,
        ready_and_waiting=True,
        source="host_api",
        observed_at=0.0,
    )


@pytest.mark.asyncio
async def test_run_fires_mergeable_flip_actuator_when_enabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion 1+4: with ``mergeable_merge_request_enabled`` on, the §5c-iv-b
    record path invokes ``deliver_handoff_cfg`` exactly once for a PR-remediation
    verdict whose PR is clean-but-unlanded and awaiting external approval, with the
    recorded verdict + PR context (the handoff names #2647)."""
    agent = make_agent()
    _arm(agent)
    agent.config.mergeable_merge_request_enabled = True
    # A real head SHA so the (pr, head_sha) serviced marker serializes.
    agent._vcs.rev_parse.return_value = "flip_head_sha"  # type: ignore[attr-defined]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_flip_readiness_awaiting_approval(),
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q), \
         patch(
             "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
         ) as mock_deliver:
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Resolved the issue")
        await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    mock_deliver.assert_called_once()
    # The handoff recruits Thomas on the cited PR at its current head.
    handoff = mock_deliver.call_args.args[1]
    assert handoff.pr_number == 2647
    assert handoff.head_sha == "flip_head_sha"
    # The readiness chokepoint supplied the merge-block reason for the ctx (the
    # actuator reads it via ``observe_pr_readiness`` for this PR's own branch).
    agent.observe_pr_readiness.assert_any_call(2647, "maestro/evolve/ep2647")


@pytest.mark.asyncio
async def test_run_skips_mergeable_flip_actuator_when_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion 4: with the flag off (default), the §5c-iv-b actuator is a no-op —
    ``deliver_handoff_cfg`` is never invoked from the record path, preserving Thomas's
    safety keystroke."""
    agent = make_agent()
    _arm(agent)
    assert agent.config.mergeable_merge_request_enabled is False
    agent._vcs.rev_parse.return_value = "flip_head_sha"  # type: ignore[attr-defined]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_flip_readiness_awaiting_approval(),
    )

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q), \
         patch(
             "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
         ) as mock_deliver:
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Resolved the issue")
        await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    mock_deliver.assert_not_called()


def test_mergeable_merge_request_config_default_is_false() -> None:
    """Criterion 5: ``Config.mergeable_merge_request_enabled`` defaults to False and
    the committed maestro.toml [loop] entry matches that default."""
    from maestro.config import Config
    from maestro.config_settings import build_config_via_pydantic

    assert Config().mergeable_merge_request_enabled is False
    built = build_config_via_pydantic(
        Path(__file__).resolve().parent.parent, env_name=None
    )
    assert built.mergeable_merge_request_enabled is False


# ── agent.run §5c-iv-c CI-failing red-read bind wiring ───────────────────


def _arm_ci_failing(agent: object) -> None:
    """Configure *agent* so a remediation reaches §5c-iv-c on a CI_FAILING PR.

    The bind reads the ``CI_FAILING`` fact straight off the host CI rollup
    (``get_pr_check_conclusion`` → ``ci_state_from_conclusion``), NOT the readiness
    chokepoint, so the host conclusion is pinned to ``"failure"``.
    """
    _arm(agent)
    agent.evolution.code_host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]


def _bash(*commands: str) -> list[ToolCall]:
    """Build a Bash tool-call list — the executed-command record red-read reads.

    The signal is sourced from the tool-call *inputs* (what actually ran), not
    the assistant prose, so these tests supply commands here and narrate freely
    in ``response``.
    """
    return [
        ToolCall(name="Bash", input={"command": c}, result="", is_error=False)
        for c in commands
    ]


# ── CI-failing red-read bind wiring (§5c-iv-c) ───────────────────────────


@pytest.mark.asyncio
async def test_run_oracle_scoped_downgrades_surfaced_unaddressed_red(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A surfaced concrete red the lap did NOT commit a fix for is stamped
    ``[red-unread]``, stays ``partial``, and fills the empty directive slot — the
    fire keys on the oracle cache plus the absence of a kept owned branch."""
    agent = make_agent()
    _arm_ci_failing(agent)
    # No committed fix this lap (kept_branch stays None ⇒ not addressed).
    agent._vcs.has_changes.return_value = False  # type: ignore[attr-defined]
    # The preflight oracle surfaced a concrete failing-check line for this PR.
    agent._ci_oracle_surfaced[2647] = True

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(
            response="Resolved the issue on the branch.",
            tool_calls=_bash(
                "git fetch origin",
                "git merge-base --is-ancestor HEAD origin/main",
            ),
        )
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    assert record.key_findings is not None
    assert _RED_UNREAD_NOTE in record.key_findings
    assert "no pytest" not in record.key_findings
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == _OPEN_THE_RED_GATE_DIRECTIVE


@pytest.mark.asyncio
async def test_run_oracle_scoped_passes_through_when_fix_committed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A surfaced red the lap committed-and-pushed a fix for passes through.

    ``_arm`` leaves ``has_changes`` True, so the remediation commits on the PR
    branch (``kept_branch`` is the pushed PR branch) and ``push_delivered`` is
    True ⇒ ``addressed_red``. The bind stands aside — no ``[red-unread]`` — the
    red was worked, not ignored."""
    agent = make_agent()
    _arm_ci_failing(agent)
    agent._ci_oracle_surfaced[2647] = True

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(
            response="Resolved the issue on the branch.",
            tool_calls=_bash(
                "git fetch origin",
                "git merge-base --is-ancestor HEAD origin/main",
            ),
        )
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert _RED_UNREAD_NOTE not in (record.key_findings or "")
    assert _RED_UNREAD_NOTE not in record.result_summary


async def _run_self_committed_lap(agent, lap_commits: int):  # type: ignore[no-untyped-def]
    """Drive a clean-tree remediation lap whose own commit count since the PR
    checkout watermark is *lap_commits* (the agent committed, or merely merged)."""
    _arm_ci_failing(agent)
    agent._vcs.has_changes.return_value = False
    agent._vcs.lap_commits_since.return_value = lap_commits
    agent._ci_oracle_surfaced[2647] = True
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(
            response="Resolved the issue on the branch.",
            tool_calls=_bash(
                "git commit -am fix",
                "git push origin HEAD:maestro/evolve/ep2647",
            ),
        )
        return await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )


@pytest.mark.asyncio
async def test_run_oracle_scoped_passes_through_when_agent_self_committed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4578: the agent committed-and-pushed its fix itself (clean tree at §5,
    no ``kept_branch``) — the lap-authored commit since the PR-head watermark
    reads as addressed, so no false ``[red-unread]``."""
    agent = make_agent()
    record = await _run_self_committed_lap(agent, 1)

    agent._vcs.lap_commits_since.assert_called_once_with(
        "maestro/evolve/ep2647", ("0" * 40,),
    )

    assert _RED_UNREAD_NOTE not in (record.key_findings or "")
    assert _RED_UNREAD_NOTE not in record.result_summary
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive != _OPEN_THE_RED_GATE_DIRECTIVE


@pytest.mark.asyncio
async def test_run_oracle_scoped_merge_only_lap_still_downgrades(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lap whose head moved only by a merge of ``origin/main`` (zero
    first-parent non-merge commits) still fires ``[red-unread]`` (#2938)."""
    agent = make_agent()
    record = await _run_self_committed_lap(agent, 0)

    assert record.key_findings is not None
    assert _RED_UNREAD_NOTE in record.key_findings


@pytest.mark.asyncio
async def test_run_oracle_scoped_stands_aside_when_no_line_surfaced(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """CI failing but the oracle surfaced no concrete line → stands aside.

    With the surfaced-line cache empty the guard finds nothing concrete to have
    ignored and leaves the verdict untouched, even on a no-committed-fix record."""
    agent = make_agent()
    _arm_ci_failing(agent)
    agent._vcs.has_changes.return_value = False  # type: ignore[attr-defined]
    # Cache deliberately left empty ⇒ oracle_surfaced_failure reads False.

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(
            response="Resolved the issue on the branch.",
            tool_calls=_bash(
                "git fetch origin",
                "git merge-base --is-ancestor HEAD origin/main",
            ),
        )
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert _RED_UNREAD_NOTE not in (record.key_findings or "")
    assert _RED_UNREAD_NOTE not in record.result_summary


def test_red_unread_note_carries_no_resolution_keyword() -> None:
    """The note keeps its ``[red-unread]`` tripwire tag and no landing keyword."""
    assert _NARRATION_RESOLUTION_RE.search(_RED_UNREAD_NOTE) is None
    assert _RED_UNREAD_NOTE.startswith("[red-unread]")


@pytest.mark.asyncio
async def test_run_oracle_scoped_downgrades_even_when_wrong_gate_ran(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lap that ran the WRONG local gate still downgrades a surfaced red.

    The regression anchor: a surfaced red the lap did not commit a fix for is
    downgraded regardless of which local subset (``pytest``/``ruff``/``mypy``)
    ran — running some gate does not exempt an ignored red."""
    agent = make_agent()
    _arm_ci_failing(agent)
    agent._vcs.has_changes.return_value = False  # type: ignore[attr-defined]
    agent._ci_oracle_surfaced[2647] = True

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_mock_q), \
         patch("maestro.agent.query_on_session", side_effect=_mock_q):
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        # A pytest invocation ran — but the wrong subset: the surfaced red was
        # never fixed and no branch commit landed.
        mock_exec.return_value = MockQueryResult(
            response="Resolved the issue: ran the tests on the branch.",
            tool_calls=_bash("python3.12 -m pytest tests/test_other.py -q"),
        )
        record = await agent.run(
            "Remediate PR #2647: address comments", source="external",
            pr_branch="maestro/evolve/ep2647",
        )

    assert record.outcome == "partial"
    assert record.key_findings is not None
    assert _RED_UNREAD_NOTE in record.key_findings
    assert "no pytest" not in record.key_findings
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == _OPEN_THE_RED_GATE_DIRECTIVE
