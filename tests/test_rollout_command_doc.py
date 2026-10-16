"""Static guards for `.claude/commands/rollout.md`.

`/rollout` moves a `burn-in:ready` proposal to `burn-in:observing`. The substate
token is carried in two places — the proposal doc's `**Status**` row and its
`## Open Proposals` row in `docs/proposals/README.md` — which
`tests/test_proposal_status_vocabulary.py` binds together. These tests pin the
command's load-bearing rules: it defers to the README Status Key for the entry
criteria rather than restating them, it makes only the `ready` → `observing`
move, it edits both carriers together and re-runs the vocabulary lint, it
grounds on a current `main`, and it never commits. The advance deploys the
production canary as one whole set (overlay line, `PROD_ONLY_CANARIES` entry,
criteria subsection, pin test) and never touches base defaults; the
`burn-in:graduated` token is routed to `/graduate-proposal`'s graduation PR.

Static-text only: reads the command doc, no `mem_fs`/subprocess/network.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.command_docs import (
    assert_no_restated_substate_criterion,
    line_containing,
)

_ADVANCE_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "rollout.md"
)


def _read() -> str:
    return _ADVANCE_DOC.read_text(encoding="utf-8")


def _section(doc: str, header: str) -> str:
    """Slice a `## ` section up to the next top-level heading (or end of doc)."""
    start = doc.index(header)
    end = doc.find("\n## ", start + len(header))
    return doc[start:] if end == -1 else doc[start:end]


def test_no_restated_substate_criterion() -> None:
    assert_no_restated_substate_criterion(_read(), "rollout.md")


def test_defers_to_status_key() -> None:
    gate = _section(_read(), "## Gate on the Status Key")

    assert "### Status Key" in gate
    assert "docs/proposals/README.md" in gate
    assert "error=entry_criterion_unmet" in gate


def test_transition_table_ready_only() -> None:
    table = _section(_read(), "## Transition table")

    assert "`burn-in:observing`" in line_containing(table, "- `burn-in:ready` →")
    assert "- `burn-in:observing` →" not in table
    assert "error=not_in_burn_in" in table
    assert "error=not_ready" in table


def test_status_mismatch_refused_before_edit() -> None:
    resolve = _section(_read(), "## Resolve the proposal")

    assert "error=status_mismatch" in resolve
    assert "/update-proposals NNN" in resolve


def test_edits_both_carriers_in_lockstep() -> None:
    doc = _read()
    apply = _section(doc, "## Apply the change")
    verify = _section(doc, "## Verify")

    assert "**Status**" in apply
    assert "## Open Proposals" in apply
    assert "tests/test_proposal_status_vocabulary.py" in verify
    assert "error=vocabulary_lint_failed" in verify


def test_switch_section_refreshes_main() -> None:
    section = _section(_read(), "## Switch to main if on a merged branch")
    on_main = section[section.index("- `main`") : section.index("- `HEAD`")]

    assert "git fetch origin main" in on_main
    assert "git merge --ff-only origin/main" in on_main
    assert "git merge-base --is-ancestor" in section
    assert "proceed on the current" in on_main.lower()


def test_never_commits_or_touches_base_defaults() -> None:
    safety = _section(_read(), "## Safety")
    never_edits = line_containing(safety, "**never edits**")

    assert "never commits, stages, pushes" in safety
    assert "`maestro/config.py`" in never_edits
    assert "`maestro.toml`" in never_edits
    assert "`PROD_ONLY_CANARY_BASELINE`" in never_edits
    assert "shipped" in safety


def test_graduation_routed_to_graduate_proposal() -> None:
    doc = _read()
    parse = _section(doc, "## Argument parse")
    safety = _section(doc, "## Safety")
    graduated = line_containing(parse, "`burn-in:graduated`")
    touches = line_containing(safety, "touches only a `burn-in:ready` proposal")

    assert "error=invalid_argument" in graduated
    assert "/graduate-proposal" in graduated
    assert "/graduate-proposal" in touches
    assert "/impl" in touches


def test_canary_deploy_writes_the_whole_set() -> None:
    deploy = _section(_read(), "## Deploy the canary")
    canary_set = deploy[deploy.index("### The canary set") :]

    for carrier in (
        "`maestro.production.toml`",
        "`PROD_ONLY_CANARIES`",
        "`docs/self-evolution.md`",
        "`tests/test_config_flag_lifecycle.py`",
        "`| **What is missing** |`",
        "`docs/proposals/README.md`",
        "`maestro.toml`",
    ):
        assert carrier in canary_set, carrier
    assert "overlay_tag" in canary_set
    assert "justification=" in canary_set
    assert "Never add it to `PROD_ONLY_CANARY_BASELINE`" in canary_set


def test_canary_deploy_refusals() -> None:
    doc = _read()
    deploy = _section(doc, "## Deploy the canary")
    gate = _section(doc, "## Gate on the Status Key")

    for error in (
        "error=ambiguous_canary_flags",
        "error=flag_not_deployable",
        "error=window_criteria_missing",
        "error=canary_unjustified",
    ):
        assert error in deploy, error
    assert "error=canary_already_deployed" in gate
    assert "Never invent a threshold" in deploy
    assert "Never flip a later-stage flag early" in deploy


def test_canary_deploy_requires_clean_tree() -> None:
    section = _section(_read(), "## Switch to main if on a merged branch")

    clean = line_containing(section, "require a clean tree")

    assert "error=dirty_tree" in clean
    assert "before any switch or edit" in clean


def test_canary_deploy_is_written_after_the_baseline() -> None:
    doc = _read()
    deploy = _section(doc, "## Deploy the canary")
    baseline = _section(doc, "## Baseline the vocabulary lint")

    assert "Nothing is written here" in deploy
    assert "error=canary_guards_red_baseline" in baseline
    assert doc.index("## Baseline the vocabulary lint") < doc.index("## Apply the change")


def test_canary_verify_reverts_whole_set_on_guard_failure() -> None:
    verify = _section(_read(), "## Verify")
    guards = line_containing(verify, "error=canary_guards_failed")

    assert "revert every canary edit" in guards
    lint = line_containing(verify, "error=vocabulary_lint_failed")
    assert "revert every canary edit" in lint
    assert "error=gates_red_after_deploy" in verify
    assert "keep the edits" in line_containing(verify, "error=gates_red_after_deploy")


def test_flag_option_parsed_and_scoped() -> None:
    doc = _read()
    parse = _section(doc, "## Argument parse")

    assert "`--flag <config_field>`" in doc
    flag_line = line_containing(parse, "`--flag` with no value")
    assert "error=invalid_argument" in flag_line


def test_argument_parse_refusals() -> None:
    parse = _section(_read(), "## Argument parse")

    assert "error=missing_proposal" in parse
    extra = line_containing(parse, "Any other extra positional token")
    assert "error=invalid_argument" in extra


def test_ungroundable_disjunct_routes_to_update_proposals() -> None:
    gate = _section(_read(), "## Gate on the Status Key")
    line = line_containing(gate, "outside the tree")

    assert "error=entry_criterion_unmet" in line
    assert "/update-proposals NNN" in line


def test_vocabulary_lint_baselined_before_edit() -> None:
    doc = _read()
    baseline = _section(doc, "## Baseline the vocabulary lint")

    assert "tests/test_proposal_status_vocabulary.py" in baseline
    assert "error=vocabulary_lint_red_baseline" in baseline
    assert doc.index("## Baseline the vocabulary lint") < doc.index("## Apply the change")
