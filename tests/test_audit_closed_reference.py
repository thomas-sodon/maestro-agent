"""Tests for rule 23 — the closed-reference self-model claim detector."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import audit_checks
from maestro.audit_checks import (
    _CLOSED_REFERENCE_FIELDS,
    _CLOSED_REFERENCE_MAX_LOOKUPS,
    AuditFinding,
    check_closed_reference_claim,
    collect_cited_issue_numbers,
    resolve_closed_refs,
    run_all_checks,
)
from maestro.consciousness import SelfModel
from maestro.vcs import SubprocessCodeHost, VCSError

FAKE_DIR = Path("/fake/workspace")


def _model(**fields: list[str]) -> SelfModel:
    model = SelfModel()
    for name, value in fields.items():
        setattr(model, name, value)
    return model


# =====================================================================
# check_closed_reference_claim
# =====================================================================


def test_fires_when_every_cited_number_closed() -> None:
    model = _model(limitations_known=["#4373, #4514 sit open across cycles"])
    findings = check_closed_reference_claim(model, frozenset({4373, 4514}))
    assert len(findings) == 1
    assert findings[0].kind == "closed_reference"
    assert findings[0].rule == "closed_reference"
    assert "limitations_known" in findings[0].statement
    assert "#4373, #4514" in findings[0].statement
    assert "REMOVE if it asserts live work" in findings[0].statement


def test_silent_when_any_cited_number_open_or_unknown() -> None:
    model = _model(limitations_known=["#4373 and #4514 are open"])
    assert check_closed_reference_claim(model, frozenset({4373})) == []


def test_silent_on_uncited_entry() -> None:
    model = _model(limitations_known=["no citations here"])
    assert check_closed_reference_claim(model, frozenset({1})) == []


def test_skips_audit_and_limitation_open_questions() -> None:
    model = _model(open_questions=[
        "[audit] stale: #5 again",
        "[limitation] #5 still blocks",
    ])
    assert check_closed_reference_claim(model, frozenset({5})) == []
    assert collect_cited_issue_numbers(model) == []


def test_skips_refuted_audit_question() -> None:
    model = _model(open_questions=["[audit refuted] #5 merged"])
    assert collect_cited_issue_numbers(model) == []


def test_long_entry_keeps_numbers_and_directive_in_160_prefix() -> None:
    entry = "x" * 200 + " #7"
    model = _model(limitations_known=[entry])
    findings = check_closed_reference_claim(model, frozenset({7}))
    prefix = findings[0].statement[:160]
    assert "#7" in prefix
    assert "REMOVE if it asserts live work" in prefix


def test_skips_non_string_entries() -> None:
    model = _model(values=[7, "#9 matters"])  # type: ignore[list-item]
    assert collect_cited_issue_numbers(model) == [9]


@pytest.mark.parametrize("field_name", _CLOSED_REFERENCE_FIELDS)
def test_covers_every_field(field_name: str) -> None:
    model = _model(**{field_name: ["see #42"]})
    findings = check_closed_reference_claim(model, frozenset({42}))
    assert len(findings) == 1
    assert findings[0].statement.startswith(field_name)


def test_per_rule_cap_is_one() -> None:
    model = _model(limitations_known=["#1 x", "#2 y"])
    assert len(check_closed_reference_claim(model, frozenset({1, 2}))) == 1


def test_lookup_cap() -> None:
    entries = [f"#{n}" for n in range(1, 30)]
    model = _model(limitations_known=entries)
    cited = collect_cited_issue_numbers(model)
    assert cited == list(range(1, _CLOSED_REFERENCE_MAX_LOOKUPS + 1))


def test_lookup_budget_admits_whole_entries_only() -> None:
    straddling = " ".join(f"#{n}" for n in range(1, 10))
    model = _model(limitations_known=[straddling, "#50 #51", "#99"])
    assert collect_cited_issue_numbers(model) == [*range(1, 10), 99]


def test_resolve_closed_refs_keeps_only_closed() -> None:
    model = _model(limitations_known=["#1 #2"])
    states = {1: "closed", 2: "open"}
    assert resolve_closed_refs(model, states.get) == frozenset({1})


def test_resolve_closed_refs_stops_on_unknown() -> None:
    model = _model(limitations_known=["#1 #2 #3"])
    lookup = MagicMock(side_effect=["closed", None, "closed"])
    assert resolve_closed_refs(model, lookup) == frozenset({1})
    assert lookup.call_count == 2


# =====================================================================
# run_all_checks dispatch
# =====================================================================


def test_run_all_checks_empty_default_byte_identical() -> None:
    model = _model(limitations_known=["#1 is open"])
    baseline = run_all_checks(model, [], 1.0)
    assert run_all_checks(model, [], 1.0, closed_refs=frozenset()) == baseline
    assert all(f.rule != "closed_reference" for f in baseline)


def test_run_all_checks_includes_rule_when_set_non_empty() -> None:
    model = _model(limitations_known=["#1 is open"])
    findings = run_all_checks(model, [], 1.0, closed_refs=frozenset({1}))
    assert [f.rule for f in findings] == ["closed_reference"]


def test_deferred_under_round_robin_clip() -> None:
    legacy = [
        AuditFinding(kind="contradicted_claim", statement=str(i), rule=name)
        for i, name in enumerate(["a_rule", "b_rule", "c_rule", "d_rule"])
    ]
    model = _model(limitations_known=["#1"])
    with (
        patch.object(audit_checks, "check_success_claim_vs_rate",
                     return_value=[legacy[0]]),
        patch.object(audit_checks, "check_stability_claim_vs_failure_streak",
                     return_value=[legacy[1]]),
        patch.object(audit_checks, "check_repeated_error_without_limitation",
                     return_value=[legacy[2]]),
        patch.object(audit_checks, "check_repeated_target_attempt",
                     return_value=[legacy[3]]),
    ):
        findings = run_all_checks(model, [], 1.0, closed_refs=frozenset({1}))
    assert len(findings) == 4
    assert all(f.rule != "closed_reference" for f in findings)


# =====================================================================
# SubprocessCodeHost.get_issue_state
# =====================================================================


@pytest.mark.parametrize("state", ["open", "closed"])
def test_get_issue_state_parses(state: str) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{state}\n",
        )
        assert host.get_issue_state(4373) == state
    cmd = mock_run.call_args[0][0]
    assert cmd == ["gh", "api", "repos/owner/repo/issues/4373", "--jq", ".state"]


def test_get_issue_state_malformed_output_is_none() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch.object(host, "_run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="weird",
        )
        assert host.get_issue_state(1) is None


def test_get_issue_state_vcs_error_is_none() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch.object(host, "_run", side_effect=VCSError("boom")):
        assert host.get_issue_state(1) is None


def test_get_issue_state_unset_repo_is_none() -> None:
    host = SubprocessCodeHost(None, workspace=FAKE_DIR)
    with patch.object(host, "_run") as run:
        assert host.get_issue_state(1) is None
    run.assert_not_called()


# =====================================================================
# _audit_self_model wiring
# =====================================================================


@pytest.mark.asyncio
async def test_audit_self_model_passes_closed_refs_when_enabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.config.audit_closed_reference_enabled = True
    agent.config.audit_impact_review_enabled = False
    agent.consciousness.mutate_self_model(
        lambda m: setattr(m, "limitations_known", ["#4373 open"]),
    )
    agent.evolution.code_host.get_issue_state = MagicMock(return_value="closed")
    with patch("maestro.audit_checks.run_all_checks", return_value=[]) as rac:
        await agent._audit_self_model(iteration=40)
    assert rac.call_args.kwargs["closed_refs"] == frozenset({4373})
    agent.evolution.code_host.get_issue_state.assert_called_once_with(4373)


@pytest.mark.asyncio
async def test_audit_self_model_no_lookup_when_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent.config.audit_closed_reference_enabled = False
    agent.config.audit_impact_review_enabled = False
    agent.consciousness.mutate_self_model(
        lambda m: setattr(m, "limitations_known", ["#4373 open"]),
    )
    agent.evolution.code_host.get_issue_state = MagicMock(return_value="closed")
    with patch("maestro.audit_checks.run_all_checks", return_value=[]) as rac:
        await agent._audit_self_model(iteration=40)
    assert rac.call_args.kwargs["closed_refs"] == frozenset()
    agent.evolution.code_host.get_issue_state.assert_not_called()
