"""Shipped-CLI and cited-CLI-evidence grounding at the submission door
(``maestro/evolution_submission.py``); split out of
``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.evolution_submission import (
    _cited_cli_tokens,
    _cli_grounding_verdict,
    _evidence_cli_grounding_verdict,
    _extract_added_cli_surface,
)
from maestro.memory import (
    EvolutionPlan,
)
from maestro.sdk import (
    TRANSIENT_SDK_CATEGORIES,
    ErrorCategory,
    SDKError,
)
from tests.conftest import MockQueryResult
from tests.test_evolution import (
    _cli_plan,
    _make,
)


def test_extract_added_cli_surface_ignores_modify_only_clauses() -> None:
    """Only add/register/new clauses contribute surface (crit. 4 helper)."""
    add_plan = _cli_plan(
        "x", "Add a --sparkline flag",
        ["Register the new maestro cost-report subcommand"],
    )
    subs, bound, unbound = _extract_added_cli_surface(add_plan)
    assert subs == {"cost-report"}
    # The title's bare "--sparkline" flag names no subcommand → unbound.
    assert unbound == {"--sparkline"}
    assert bound == set()

    modify_plan = _cli_plan(
        "y", "Fix the --json output of cost-report",
        ["Correct the broken --json serialization in the existing command"],
    )
    assert _extract_added_cli_surface(modify_plan) == (set(), set(), set())


def test_cli_grounding_verdict_none_for_add_clause_referencing_shipped_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enhancing a shipped command is not proposing it (issue #4374)."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _cli_plan(
        "x", "Improve cost report",
        ["Add a per-phase breakdown table to the maestro cost-report output"],
    )
    assert _extract_added_cli_surface(plan)[0] == set()
    assert _cli_grounding_verdict(plan) is None


def test_cli_grounding_verdict_none_for_new_subcommand_citing_shipped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new subcommand that cites a shipped one is not refused (issue #4374)."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"measure-gate-contention"}, {}, set()),
    )
    plan = _cli_plan(
        "x", "Add measure-x",
        ["Add a new `measure-x` subcommand that mirrors maestro measure-gate-contention"],
    )
    assert _cli_grounding_verdict(plan) is None


def test_extract_added_cli_surface_counts_subcommand_as_verb_object() -> None:
    """A ``maestro <sub>`` directly after the intent verb is still proposed."""
    for text in (
        "Add maestro cost-report",
        "Expose a `maestro cost-report` subcommand",
        "Register the new maestro cost-report subcommand",
    ):
        plan = _cli_plan("x", text, [])
        assert _extract_added_cli_surface(plan)[0] == {"cost-report"}


def test_extract_added_cli_surface_maestro_literal_is_case_sensitive() -> None:
    """Capitalised "Maestro" prose is not a subcommand invocation."""
    for text in (
        "Add Maestro status reporting to the Telegram digest",
        "Create Maestro run summaries",
    ):
        plan = _cli_plan("x", text, [])
        assert _extract_added_cli_surface(plan)[0] == set()


def test_extract_added_cli_surface_new_is_not_a_verb() -> None:
    """"the new maestro <sub> output" is a reference, not an add object."""
    plan = _cli_plan("x", "Show the new maestro cost-report output in the digest", [])
    assert _extract_added_cli_surface(plan)[0] == set()


def test_extract_added_cli_surface_counts_named_subcommand() -> None:
    """``maestro <sub> subcommand`` in an intent clause is proposed."""
    plan = _cli_plan("x", "Expose it as the maestro cost-report subcommand", [])
    assert _extract_added_cli_surface(plan)[0] == {"cost-report"}


def test_cli_grounding_verdict_none_when_coordinated_subcommand_unshipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A novel subcommand in a coordinated list vetoes the refusal."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    for text in (
        "Add maestro cost-report and maestro foo-new subcommands",
        "Register `maestro cost-report` and `maestro foo-new`",
    ):
        assert _cli_grounding_verdict(_cli_plan("x", text, [])) is None


def test_extract_added_cli_surface_binds_flag_to_named_subcommand() -> None:
    """A flag named alongside a subcommand is bound as a (sub, flag) pair."""
    plan = _cli_plan(
        "z", "Add a --json flag to the journal subcommand",
        ["Register the new --json flag on deed-ledger"],
    )
    subs, bound, unbound = _extract_added_cli_surface(plan)
    assert subs == set()
    assert ("journal", "--json") in bound
    assert ("deed-ledger", "--json") in bound
    assert unbound == set()


def test_extract_added_cli_surface_hyphenated_token_supplies_no_intent() -> None:
    """An intent word inside a hyphenated token or flag is not add-intent."""
    fix_plan = _cli_plan(
        "x", "Fix the sort order of maestro measure-new-thing output", [],
    )
    assert _extract_added_cli_surface(fix_plan) == (set(), set(), set())
    flag_plan = _cli_plan("y", "Correct the --new-format output of cost-report", [])
    assert _extract_added_cli_surface(flag_plan) == (set(), set(), set())
    bare_flag_plan = _cli_plan("w", "Correct the --new output of cost-report", [])
    assert _extract_added_cli_surface(bare_flag_plan) == (set(), set(), set())

    add_plan = _cli_plan("z", "Add a --json flag to maestro measure-new-thing", [])
    _subs, bound, _unbound = _extract_added_cli_surface(add_plan)
    assert bound == {("measure-new-thing", "--json")}


@pytest.mark.asyncio
async def test_submit_keeps_cli_modify_plan_with_intent_word_in_token(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-only clause naming a shipped ``measure-new-*`` token stays pending."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"measure-new-thing"}, {}, set()),
    )
    plan = _cli_plan(
        "cli-modify", "Fix the sort order of `maestro measure-new-thing` output",
        [],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-modify")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_keeps_cli_enhancement_plan_citing_shipped_command(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An "add … to the maestro <sub> output" enhancement stays pending."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _cli_plan(
        "cli-enhance", "Improve cost report",
        ["Add a per-phase breakdown table to the maestro cost-report output"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-enhance")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_refuses_already_shipped_cli_surface(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 1: a plan proposing only already-shipped CLI tokens is refused.

    Every add-intent token it names is registered in cli.py, so it lands
    ``obsolete`` via the shared refusal path — never a second review burden.
    """
    e, m, _c, j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: (
            {"cost-report", "deed-ledger"},
            {"deed-ledger": {"--json"}, "cost-report": {"--json"}},
            set(),
        ),
    )
    plan = _cli_plan(
        "cli-shipped", "Add a --json flag to deed-ledger",
        ["Register the new --json flag on the deed-ledger subcommand"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-shipped")
    assert got is not None
    assert got.status == "obsolete"
    notes = j.read_events_by_type("evolution_note")
    assert any("already" in n.content for n in notes)


@pytest.mark.asyncio
async def test_submit_allows_novel_cli_token(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2: at least one un-shipped token → the plan is NOT refused."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {"cost-report": {"--json"}}, set()),
    )
    plan = _cli_plan(
        "cli-novel", "Add a --sparkline flag to cost-report",
        ["Register a new --sparkline flag rendering a trend line"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-novel")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_ignores_non_cli_plan(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: a plan not touching cli.py is never refused by this check."""
    called = False

    def _surface(
        source: str | None = None,
    ) -> tuple[set[str], dict[str, set[str]], set[str]]:
        nonlocal called
        called = True
        return {"cost-report"}, {"cost-report": {"--json"}}, set()

    monkeypatch.setattr("maestro.evolution_submission._shipped_cli_surface", _surface)
    e, m, _c, _j = _make(mem_fs)
    plan = _cli_plan(
        "cli-untouched", "Add a --json flag to the dashboard route",
        ["Register the new --json flag"],
        files=["maestro/dashboard/routes.py"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-untouched")
    assert got is not None
    assert got.status == "pending_review"
    # The shipped surface is never even consulted when cli.py is untouched.
    assert called is False


@pytest.mark.asyncio
async def test_submit_allows_cli_modify_only_plan(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4: a modify/fix-only cli.py plan (no add-intent) passes through.

    It references an already-shipped token to *change* its behavior, not to
    register it, so no add-intent tokens are extracted and the guard does not
    obsolete a legitimate CLI-modification plan.
    """
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {"cost-report": {"--json"}}, set()),
    )
    plan = _cli_plan(
        "cli-modify", "Fix the --json output of cost-report",
        ["Correct the broken --json serialization in the existing subcommand"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-modify")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_shipped_cli_guard_idempotent_retry(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-submission of an already-obsoleted shipped-CLI plan writes no 2nd note.

    A pre-persisted ``obsolete`` row under the candidate id escapes the active
    title-exact dedup, so the candidate reaches the shipped-CLI guard; the
    ``save_evolution_plan`` there raises ``DuplicatePlanError`` and the
    idempotent path returns without a second journal note.
    """
    e, m, _c, j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"deed-ledger"}, {"deed-ledger": {"--json"}}, set()),
    )
    m.save_evolution_plan(EvolutionPlan(
        plan_id="cli-dup", title="Some prior title",
        category="capability", rationale="already recorded obsolete",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="obsolete", created_at="2025-01-01T00:00:00Z",
    ))
    candidate = _cli_plan(
        "cli-dup", "Add a --json flag to deed-ledger",
        ["Register the new --json flag on deed-ledger"],
    )
    await e.submit_for_review(candidate)  # must not raise

    matching = [p for p in m.get_all_plans() if p.plan_id == "cli-dup"]
    assert len(matching) == 1
    assert matching[0].status == "obsolete"
    assert len(j.read_events_by_type("evolution_note")) == 0


@pytest.mark.asyncio
async def test_submit_allows_flag_novel_for_target_subcommand(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flag registered on one subcommand, proposed for another, is NOT refused.

    Regression for the flat-token false positive: ``--json`` is registered on
    ``cost-report`` but the plan proposes it for ``journal`` (which lacks it).
    A subcommand-agnostic token set would obsolete this legitimate capability
    work; the subcommand-bound surface treats it as novel and lets it through.
    """
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: (
            {"journal", "cost-report"},
            {"journal": {"-n", "--count"}, "cost-report": {"--json"}},
            set(),
        ),
    )
    plan = _cli_plan(
        "cli-cross-sub", "Add a --json flag to the journal subcommand",
        ["Register the new --json flag on journal so entries print as JSON"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-cross-sub")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_allows_flag_novel_against_real_cli_surface(
    mem_fs: dict[str, str],
) -> None:
    """End-to-end against the real cli.py: ``journal --json`` is novel surface.

    No monkeypatch — the guard reads the on-disk ``cli.py``. ``--json`` exists
    on other subcommands but not ``journal``, so proposing it for ``journal``
    must reach review rather than being silently obsoleted.
    """
    e, m, _c, _j = _make(mem_fs)
    plan = _cli_plan(
        "cli-real-novel", "Add a --json flag to the journal subcommand",
        ["Register the new --json flag on the journal subcommand"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-real-novel")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_refuses_already_shipped_subcommand(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proposed ``maestro <sub>`` already in the global namespace is refused."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _cli_plan(
        "cli-dup-sub", "Add a cost-report view",
        ["Register the new maestro cost-report subcommand"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cli-dup-sub")
    assert got is not None
    assert got.status == "obsolete"


def test_cli_grounding_verdict_refuses_unbound_global_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound flag matching a shipped *global* flag counts as already-shipped."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: (set(), {}, {"-v", "--verbose"}),
    )
    plan = _cli_plan(
        "g", "Add a new --verbose flag",
        ["Introduce a --verbose top-level flag"],
    )
    assert _cli_grounding_verdict(plan) == {"--verbose"}


def test_cli_grounding_verdict_none_for_unbound_non_global_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound flag that is not a global flag is novel → verdict ``None``."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {"cost-report": {"--json"}}, set()),
    )
    plan = _cli_plan("h", "Add a --json flag", ["Register a new --json flag"])
    assert _cli_grounding_verdict(plan) is None


def test_cli_grounding_verdict_none_for_novel_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proposed ``maestro <sub>`` absent from the shipped namespace → ``None``."""
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _cli_plan(
        "i", "Add a foo-report view",
        ["Register the new maestro foo-report subcommand"],
    )
    assert _cli_grounding_verdict(plan) is None


# ---------------------------------------------------------------------------
# Cited-CLI-evidence grounding — refuse a plan whose rationale leans on a
# ``maestro <token>`` sensor that is neither shipped nor added by the plan
# (the 'nonexistent-evidence premise' DOA rejection class).
# ---------------------------------------------------------------------------


def _evidence_plan(
    plan_id: str,
    rationale: str,
    *,
    changes: list[str] | None = None,
    files: list[str] | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=f"Plan {plan_id}",
        category="capability", rationale=rationale,
        proposed_changes=changes if changes is not None else ["do a thing"],
        files_affected=files if files is not None else ["maestro/agent.py"],
        expected_impact="", created_at="2025-01-01T00:00:00Z",
    )


def test_cited_cli_tokens_extracts_prose_and_backtick_invocations() -> None:
    """A ``maestro <token>`` invocation is mined in prose or backtick-wrapped."""
    rationale = (
        "Running `maestro measure-premise-accuracy` shows a trend; running "
        "maestro cost-report confirms it."
    )
    assert _cited_cli_tokens(rationale) == {
        "measure-premise-accuracy", "cost-report",
    }


def test_cited_cli_tokens_ignores_bare_hyphenated_concept_names() -> None:
    """A hyphenated concept name in backticks (no ``maestro`` prefix) is NOT mined.

    Regression against the false-positive that would obsolete every legitimate
    plan referencing a hyphenated invariant/concept name in backticks: only a
    ``maestro <token>`` invocation counts as a cited CLI sensor.
    """
    rationale = (
        "The `merge-truth` chokepoint and `human-in-the-loop` model bind it; "
        "`source_task_id` too."
    )
    assert _cited_cli_tokens(rationale) == set()


def test_cited_cli_tokens_ignores_non_hyphenated_and_underscored() -> None:
    """Bare words after ``maestro`` and underscore identifiers contribute nothing."""
    rationale = "maestro evolve loops; the `is_pr_settled` chokepoint binds it."
    assert _cited_cli_tokens(rationale) == set()


def test_cited_cli_tokens_ignores_maestro_prefixed_concept_names() -> None:
    """A ``maestro <hyphenated-concept>`` phrase is NOT a cited CLI subcommand.

    Regression against the false positive on PR #3584: this codebase's
    vocabulary routinely qualifies a hyphenated concept noun with the agent's
    own name — ``maestro self-model``, ``maestro red-root``, ``maestro
    merge-truth``, ``maestro burn-rate``. None of these are shipped CLI
    subcommands, so mining them as cited CLI evidence obsoletes an entirely
    legitimate, non-CLI plan. Only a hyphenated *subcommand token* (one whose
    verb prefix is an actual maestro read-surface verb) may be mined.
    """
    rationale = (
        "The maestro self-model binds identity; the maestro red-root preflight "
        "and maestro merge-truth chokepoint gate it, and maestro burn-rate "
        "throttles the loop."
    )
    assert _cited_cli_tokens(rationale) == set()


@pytest.mark.asyncio
async def test_submit_refuses_cited_phantom_cli_evidence(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 1: a plan citing an unshipped ``maestro <token>`` is refused.

    The rationale leans on a ``measure-phantom-sensor`` read surface absent
    from the shipped surface and not added by the plan's own diff, so it lands
    ``obsolete`` via the shared refusal path — never a review burden.
    """
    e, m, _c, j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report", "deed-ledger"}, {}, set()),
    )
    plan = _evidence_plan(
        "cited-phantom",
        "Recent laps show waste; `maestro measure-phantom-sensor` proves the "
        "pattern recurs, so this closes the valve it surfaces.",
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cited-phantom")
    assert got is not None
    assert got.status == "obsolete"
    notes = j.read_events_by_type("evolution_note")
    assert any("measure-phantom-sensor" in n.content for n in notes)


@pytest.mark.asyncio
async def test_submit_allows_cited_shipped_cli_evidence(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2: citing a genuinely-shipped ``maestro <token>`` is NOT refused."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report", "measure-premise-accuracy"}, {}, set()),
    )
    plan = _evidence_plan(
        "cited-real",
        "Running `maestro measure-premise-accuracy` (already on main) shows the "
        "premise-accuracy trend; this change acts on it.",
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cited-real")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_allows_plan_citing_maestro_prefixed_concept(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-CLI plan whose rationale uses ``maestro <concept>`` prose is NOT refused.

    Reproduces the PR #3584 false positive: the rationale contains hyphenated
    concept nouns qualified by the agent's own name (``maestro self-model``,
    ``maestro merge-truth``) — none is a shipped CLI subcommand and the plan
    touches no ``maestro/cli.py``. The evidence-grounding guard must fail open
    here; wrongly treating the concept nouns as cited-but-unshipped CLI
    subcommands obsoletes an entirely legitimate capability plan.
    """
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report", "measure-premise-accuracy"}, {}, set()),
    )
    plan = _evidence_plan(
        "concept-prose",
        "The maestro self-model drifts across laps; the maestro merge-truth "
        "chokepoint and maestro red-root preflight already bind delivery, so "
        "this change tightens the self-model audit — no CLI surface involved.",
    )
    await e.submit_for_review(plan)

    got = m.get_plan("concept-prose")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_allows_cited_token_added_by_own_diff(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: a cited token the plan's own cli.py diff adds is exempt.

    The token is not shipped, but the plan introduces it via a maestro/cli.py
    change, so the citation is a legitimate forward reference — not refused.
    """
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _evidence_plan(
        "cited-added",
        "This adds the maestro measure-new-thing read surface; the "
        "`measure-new-thing` sensor will then make the pattern legible.",
        changes=["Register the new maestro measure-new-thing subcommand"],
        files=["maestro/cli.py"],
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cited-added")
    assert got is not None
    assert got.status == "pending_review"


_UNPREFIXED_RATIONALE = (
    "Once shipped, `maestro measure-new-thing` will show the stranded ratio."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "changes"),
    [
        ("Plan up", ["Add a `measure-new-thing` subcommand to maestro/cli.py"]),
        ("Plan up", ["register a measure-new-thing subparser"]),
        ("Add measure-new-thing", ["wire the reader"]),
    ],
)
async def test_submit_allows_cited_token_added_without_maestro_prefix(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    title: str, changes: list[str],
) -> None:
    """#4109: a cli.py plan naming its new subcommand without ``maestro `` is exempt."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _evidence_plan(
        "unprefixed", _UNPREFIXED_RATIONALE,
        changes=changes, files=["maestro/cli.py"],
    )
    plan.title = title
    await e.submit_for_review(plan)

    got = m.get_plan("unprefixed")
    assert got is not None
    assert got.status == "pending_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "files"),
    [
        (["Tidy the argument help text"], ["maestro/cli.py"]),
        (["Add a `measure-new-thing` subcommand"], ["maestro/agent.py"]),
        (["Add a `measure-new-thing-extra` subcommand"], ["maestro/cli.py"]),
        (["Fix the sort order of `measure-new-thing` output"], ["maestro/cli.py"]),
    ],
    ids=["never-named", "cli-py-absent", "longer-token-only", "modify-only"],
)
async def test_submit_refuses_cli_plan_whose_changes_never_name_cited_token(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    changes: list[str], files: list[str],
) -> None:
    """#4109 controls: the exemption needs a cli.py change naming the whole token."""
    e, m, _c, _j = _make(mem_fs)
    monkeypatch.setattr(
        "maestro.evolution_submission._shipped_cli_surface",
        lambda source=None: ({"cost-report"}, {}, set()),
    )
    plan = _evidence_plan(
        "unnamed", _UNPREFIXED_RATIONALE, changes=changes, files=files,
    )
    await e.submit_for_review(plan)

    got = m.get_plan("unnamed")
    assert got is not None
    assert got.status == "obsolete"


@pytest.mark.asyncio
async def test_submit_allows_plan_citing_no_cli_token(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4: a rationale citing no CLI token is unaffected by this check."""
    called = False

    def _surface(
        source: str | None = None,
    ) -> tuple[set[str], dict[str, set[str]], set[str]]:
        nonlocal called
        called = True
        return {"cost-report"}, {}, set()

    monkeypatch.setattr("maestro.evolution_submission._shipped_cli_surface", _surface)
    e, m, _c, _j = _make(mem_fs)
    plan = _evidence_plan(
        "cited-none",
        "Recent task history shows a repeated pattern; this change addresses "
        "it directly with no sensor citation.",
    )
    await e.submit_for_review(plan)

    got = m.get_plan("cited-none")
    assert got is not None
    assert got.status == "pending_review"
    # No cited token → the shipped surface is never even consulted.
    assert called is False


def test_evidence_verdict_none_when_no_tokens_cited() -> None:
    """The verdict short-circuits to ``None`` before any surface read."""
    plan = _evidence_plan("v-none", "A rationale with no maestro CLI citation.")
    assert _evidence_cli_grounding_verdict(plan) is None


def _make_rejected_plan(plan_id: str = "p1", reason: str = "Too aggressive") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title="Rejected Plan",
        category="capability", rationale="Bad idea", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", created_at="2025-01-01T00:00:00Z",
        rejection_reason=reason,
    )


@pytest.mark.asyncio
async def test_learn_from_rejection(mem_fs: dict[str, str]) -> None:
    e, m, c, j = _make(mem_fs)
    plan = _make_rejected_plan()

    update_json = json.dumps({
        "limitations_known": ["I can be too aggressive in proposals"],
    })

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response=update_json)
        mock_evo_q.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(plan, "Too aggressive")

    mock_cons_q.assert_called_once()
    prompt = mock_cons_q.call_args.kwargs.get(
        "prompt", mock_cons_q.call_args[1].get("prompt", ""),
    )
    assert "Too aggressive" in prompt

    entries = j.read_events_by_type("realization")
    assert len(entries) == 1
    assert "rejected" in entries[0].content.lower()

    model = c.load_self_model()
    assert "I can be too aggressive in proposals" in model.limitations_known

    # Classifier ran once and the structured record was persisted.
    assert mock_evo_q.call_count == 1
    # Fallback path must pass the classifier instruction as the system prompt
    # so the model receives domain context identical to the session path.
    assert mock_evo_q.call_args.kwargs["system"] == (
        "You classify evolution plan rejection reasons."
    )
    rejections = m.get_recent_rejections()
    assert len(rejections) == 1
    assert rejections[0].plan_id == "p1"
    assert rejections[0].rejection_category == "scope_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("response, expected", [
    ("quality_issue", "quality_issue"),
    # Decorated reply: scan all tokens, strip trailing punctuation.
    ("The category is scope_too_large.", "scope_too_large"),
    # Leading preamble before the label.
    ("I'd say: duplicate", "duplicate"),
    # Junk → falls back to "other".
    ("blahblah nonsense", "other"),
    # Empty response.
    ("", "other"),
])
async def test_learn_from_rejection_classifier_parses(
    mem_fs: dict[str, str], response: str, expected: str,
) -> None:
    e, m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id=f"p_{expected}")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_evo_q.return_value = MockQueryResult(response=response)
        await e.learn_from_rejection(plan, plan.rejection_reason or "")

    [rec] = m.get_recent_rejections()
    assert rec.rejection_category == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    sorted(TRANSIENT_SDK_CATEGORIES, key=lambda c: c.value),
)
async def test_learn_from_rejection_classifier_failure_defaults_to_other(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    category: ErrorCategory,
) -> None:
    """A *transient* classifier failure must not prevent persistence — the
    record still lands with ``other`` and a warning is logged, and
    ``update_self_model`` still runs.

    Parameterized over every member of ``TRANSIENT_SDK_CATEGORIES`` so the
    ``other`` fallback stays pinned to exactly that set: a category added to
    the policy without being considered here would show up as a new case,
    and a category removed from it falls to the propagation test below.
    """
    e, m, c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id=f"err_{category.value}")

    update_json = json.dumps({
        "limitations_known": ["I can be too aggressive"],
    })

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q, caplog.at_level("WARNING", logger="maestro"):
        mock_cons_q.return_value = MockQueryResult(response=update_json)
        mock_evo_q.side_effect = SDKError(
            "transient classifier failure", category=category,
        )
        await e.learn_from_rejection(plan, "Too aggressive")

    [rec] = m.get_recent_rejections()
    assert rec.rejection_category == "other"
    assert any(
        "Rejection classifier failed" in r.message for r in caplog.records
    )
    # Self-model update still happened.
    model = c.load_self_model()
    assert "I can be too aggressive" in model.limitations_known


@pytest.mark.asyncio
@pytest.mark.parametrize("category", [
    ErrorCategory.AUTH,
    ErrorCategory.UNKNOWN,
    ErrorCategory.NETWORK,
    ErrorCategory.TIMEOUT,
])
async def test_learn_from_rejection_non_transient_classifier_error_propagates(
    mem_fs: dict[str, str], category: ErrorCategory,
) -> None:
    """A classifier ``SDKError`` outside ``TRANSIENT_SDK_CATEGORIES`` must
    crash loudly rather than be softened into a durable ``other`` record.

    The swallow is keyed on the error *category*, not the exception class:
    an expired token (AUTH) or a programming defect (UNKNOWN) is
    unrecoverable everywhere else in the loop, and rejection learning runs
    once per plan — a wrongly-defaulted record is never re-derived, so it
    would silently poison the ``RejectionCategory`` aggregates ``reflect()``
    reads (#3795).
    """
    e, m, _c, j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id=f"hard_{category.value}")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_evo_q.side_effect = SDKError(
            "unrecoverable classifier failure", category=category,
        )
        with pytest.raises(SDKError):
            await e.learn_from_rejection(plan, "Too aggressive")

    assert m.get_recent_rejections() == []
    # The realization guard must NOT have been written: it short-circuits
    # every later call, so setting it before a re-raise would leave the plan
    # with no record and no self-model update forever.
    assert [
        entry for entry in j.read_events_by_type("realization")
        if entry.triggered_by == f"rejection:{plan.plan_id}"
    ] == []


@pytest.mark.asyncio
async def test_learn_from_rejection_bare_timeout_propagates(
    mem_fs: dict[str, str],
) -> None:
    """A bare ``TimeoutError`` reaches this caller only straight from the SDK
    transport, which must "propagate with its original semantics" — so it is
    no longer softened into an ``other`` record either (#3795).
    """
    e, m, _c, j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="bare_timeout1")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_evo_q.side_effect = TimeoutError("transport timeout")
        with pytest.raises(TimeoutError, match="transport timeout"):
            await e.learn_from_rejection(plan, "Too aggressive")

    assert m.get_recent_rejections() == []
    assert [
        entry for entry in j.read_events_by_type("realization")
        if entry.triggered_by == f"rejection:{plan.plan_id}"
    ] == []


@pytest.mark.asyncio
async def test_learn_from_rejection_retries_after_non_transient_error(
    mem_fs: dict[str, str],
) -> None:
    """A propagated classifier error must leave the call fully retryable.

    The realization journal entry is this method's idempotency guard, so it
    is written only after the classifier settles.  A later call therefore
    re-runs the classifier and persists the *real* category instead of
    short-circuiting on a guard left behind by the failed attempt (#3795).
    """
    e, m, _c, j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="retry_after_hard1")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_evo_q.side_effect = SDKError(
            "expired token", category=ErrorCategory.AUTH,
        )
        with pytest.raises(SDKError):
            await e.learn_from_rejection(plan, "Too aggressive")

        mock_evo_q.side_effect = None
        mock_evo_q.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(plan, "Too aggressive")

    records = m.get_recent_rejections()
    assert len(records) == 1
    assert records[0].rejection_category == "scope_too_large"
    entries = [
        ent for ent in j.read_events_by_type("realization")
        if ent.triggered_by == f"rejection:{plan.plan_id}"
    ]
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_learn_from_rejection_parser_bug_propagates(
    mem_fs: dict[str, str],
) -> None:
    """A non-SDK failure in the response-parsing path (a parser regression,
    not a flaky classifier call) must surface loudly rather than be swallowed
    as the "classifier failed" warning and recorded as ``other`` (#1404).

    The narrowed ``except SDKError`` covers only the classifier call; the parse chain runs outside it, so an ``AttributeError``
    from ``classification.response`` propagates out of ``learn_from_rejection``
    and no rejection record is written.
    """

    class _ExplodingResponse:
        """Stands in for a QueryResult whose ``.response`` access is buggy."""

        @property
        def response(self) -> str:
            raise AttributeError("simulated parser regression")

    e, m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="parse_bug1")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_evo_q.return_value = _ExplodingResponse()
        with pytest.raises(AttributeError, match="simulated parser regression"):
            await e.learn_from_rejection(plan, "Too aggressive")

    # The parser bug aborted before persistence — no record was written, and
    # the bug was not masked as a recorded ``other`` category.
    assert m.get_recent_rejections() == []


@pytest.mark.asyncio
async def test_learn_from_rejection_is_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """Calling ``learn_from_rejection`` twice for the same plan must produce
    only one realization journal entry, one ``update_self_model`` SDK call,
    and one structured record — the second call short-circuits before the
    classifier runs.
    """
    e, m, _c, j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="dup1")

    update_json = json.dumps({
        "limitations_known": ["I can be too aggressive in proposals"],
    })

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response=update_json)
        mock_evo_q.return_value = MockQueryResult(response="duplicate")
        await e.learn_from_rejection(plan, "Too aggressive")
        await e.learn_from_rejection(plan, "Too aggressive")

    # Both update_self_model and the classifier should each be called exactly
    # once — second invocation short-circuits on the journal guard.
    assert mock_cons_q.call_count == 1
    assert mock_evo_q.call_count == 1

    entries = [
        ent for ent in j.read_events_by_type("realization")
        if ent.triggered_by == f"rejection:{plan.plan_id}"
    ]
    assert len(entries) == 1
    assert len(m.get_recent_rejections()) == 1


@pytest.mark.asyncio
async def test_learn_from_rejection_short_circuits_on_existing_realization(
    mem_fs: dict[str, str],
) -> None:
    """If a realization journal entry for this rejection already exists
    (e.g. previous loop iteration journalled it but the rejection_learned
    flag was never set due to a process crash mid-call),
    ``learn_from_rejection`` must not re-journal, must not re-call
    ``update_self_model``, must not re-run the classifier, and must not
    write a duplicate structured record.
    """
    e, m, _c, j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="prev1")
    # Pre-seed the event log with a realization for this exact plan id.
    j.record_event(
        content="Earlier realization for this rejection.",
        entry_type="realization",
        triggered_by=f"rejection:{plan.plan_id}",
    )

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        await e.learn_from_rejection(plan, "Too aggressive")

    mock_cons_q.assert_not_called()
    mock_evo_q.assert_not_called()
    entries = [
        ent for ent in j.read_events_by_type("realization")
        if ent.triggered_by == f"rejection:{plan.plan_id}"
    ]
    assert len(entries) == 1
    assert m.get_recent_rejections() == []


@pytest.mark.asyncio
async def test_learn_from_rejection_session_path_uses_query_on_session(
    mem_fs: dict[str, str],
) -> None:
    """Classifier is dispatched on ``query_on_session`` when a session is
    provided so the identity prompt is cached across rejections in one
    cycle. The one-shot ``query()`` fallback must not be touched.
    """
    e, m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="sess1")

    update_json = json.dumps({
        "limitations_known": ["scope was too broad"],
    })

    fake_session = MagicMock(name="ClaudeSDKClient")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q, patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_cons_q.return_value = MockQueryResult(response=update_json)
        mock_qos.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(
            plan, "Too aggressive", session=fake_session,
        )

    mock_qos.assert_awaited_once()
    assert mock_qos.call_args.args[0] is fake_session
    assert mock_qos.call_args.kwargs["caller"] == "rejection_classifier"
    mock_evo_q.assert_not_called()
    [rec] = m.get_recent_rejections()
    assert rec.rejection_category == "scope_too_large"


@pytest.mark.asyncio
async def test_learn_from_rejection_self_model_session_path(
    mem_fs: dict[str, str],
) -> None:
    """When a *self_model_session* is supplied, the self-model update is
    dispatched on it and the consciousness one-shot ``query()`` is never
    awaited — the interprocedural session drop of #3803.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="sm_sess")

    classifier_session = MagicMock(name="ClassifierSession")
    self_model_session = MagicMock(name="SelfModelSession")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_cons_qos, patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_cons_qos.return_value = MockQueryResult(response=json.dumps({
            "limitations_known": ["scope was too broad"],
        }))
        mock_qos.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(
            plan, "Too aggressive",
            session=classifier_session,
            self_model_session=self_model_session,
        )

    mock_cons_q.assert_not_called()
    mock_cons_qos.assert_awaited_once()
    assert mock_cons_qos.call_args.args[0] is self_model_session
    assert mock_cons_qos.call_args.kwargs["caller"] == "update_self_model"
    # The classifier keeps its own, separate session.
    assert mock_qos.call_args.args[0] is classifier_session


@pytest.mark.asyncio
async def test_learn_from_rejection_no_self_model_session_uses_bare_query(
    mem_fs: dict[str, str],
) -> None:
    """Without a *self_model_session* the documented one-shot ``query()``
    arm of ``update_self_model`` is unchanged (#3803)."""
    e, _m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="sm_bare")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_cons_qos, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q:
        mock_cons_q.return_value = MockQueryResult(response=json.dumps({
            "limitations_known": ["scope was too broad"],
        }))
        mock_evo_q.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(plan, "Too aggressive")

    mock_cons_q.assert_awaited_once()
    mock_cons_qos.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response, expected", [
    ("quality_issue", "quality_issue"),
    # Decorated reply: scan all tokens, strip trailing punctuation.
    ("The category is scope_too_large.", "scope_too_large"),
    # Leading preamble before the label.
    ("I'd say: duplicate", "duplicate"),
    # Junk → falls back to "other".
    ("blahblah nonsense", "other"),
    # Empty response.
    ("", "other"),
])
async def test_learn_from_rejection_session_path_classifier_parses(
    mem_fs: dict[str, str], response: str, expected: str,
) -> None:
    """Session-path classifier parsing must behave identically to the
    one-shot ``query()`` fallback — same response/expected pairs as
    ``test_learn_from_rejection_classifier_parses`` routed through
    ``query_on_session`` instead.  Pins #1725 end-to-end.
    """
    e, m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id=f"sp_{expected}")

    fake_session = MagicMock(name="ClaudeSDKClient")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ) as mock_evo_q, patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_cons_q.return_value = MockQueryResult(response="{}")
        mock_qos.return_value = MockQueryResult(response=response)
        await e.learn_from_rejection(
            plan, plan.rejection_reason or "", session=fake_session,
        )

    mock_qos.assert_awaited_once()
    mock_evo_q.assert_not_called()
    [rec] = m.get_recent_rejections()
    assert rec.rejection_category == expected


@pytest.mark.asyncio
async def test_learn_from_rejection_session_path_includes_classifier_instruction(
    mem_fs: dict[str, str],
) -> None:
    """Regression test for #1725: the session-attached path must include the
    classifier-specific instruction 'You classify evolution plan rejection
    reasons' in the prompt sent to ``query_on_session``.  The one-shot
    ``query()`` path passes this as the ``system`` kwarg; the session path
    cannot set a per-turn system prompt, so the instruction must be prepended
    to the user prompt instead.  Without the fix the model receives only the
    generic identity prompt from the session and may respond in agent-voice
    prose instead of a single category token.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = _make_rejected_plan(plan_id="sess_instr")

    update_json = json.dumps({
        "limitations_known": ["scope was too broad"],
    })

    fake_session = MagicMock(name="ClaudeSDKClient")

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_cons_q, patch(
        "maestro.evolution.query", new_callable=AsyncMock,
    ), patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_cons_q.return_value = MockQueryResult(response=update_json)
        mock_qos.return_value = MockQueryResult(response="scope_too_large")
        await e.learn_from_rejection(
            plan, "Too aggressive", session=fake_session,
        )

    mock_qos.assert_awaited_once()
    prompt_sent = mock_qos.call_args.args[1]
    assert "You classify evolution plan rejection reasons" in prompt_sent, (
        "Session path must include the classifier instruction in the prompt "
        f"sent to query_on_session, but got: {prompt_sent!r}"
    )
