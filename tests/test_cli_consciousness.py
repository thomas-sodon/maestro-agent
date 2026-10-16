"""consciousness-report and workspace-counterfactual; split out of
``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.cli import (
    _build_conscious_episode_backend,
    cmd_consciousness_report,
    cmd_workspace_counterfactual,
)
from maestro.config import Config
from maestro.consciousness import Consciousness, claim_key
from maestro.memory import (
    Memory,
)
from tests.conftest import capture_stdout
from tests.test_cli import (
    _make_cli_version,
    _make_config,
)

# ── consciousness-report (measured operational-awareness surface) ───────────────


def _seed_consciousness(config: Config, mem_fs: dict[str, str]) -> None:
    """Seed every input so each implemented row renders a measured reading.

    Perceptual grounding: one resolved, one wrong-polarity, and one uncited
    claim. Metacognitive calibration: one stamped, measured, resolved-correct
    bet. Narration→actuation conversion: one PR-grounded and one SHA-grounded
    actuation, both settled by the offline ledgers (a merged PR row and an
    archived commit SHA).
    """
    from maestro.audit_checks_calibration import (
        JsonlPredictionLedger,
        record_reflection_prediction,
        record_reflection_verdict,
    )

    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.capabilities_known = ["ships features", "lands prs", "writes docs"]
    model.claim_evidence = {
        claim_key("ships features"): {
            "citation": "task aabbccddeeff", "kind": "TASK",
            "status": "resolved", "recorded_at": "t",
        },
        claim_key("lands prs"): {
            "citation": "task 112233445566", "kind": "TASK",
            "status": "wrong_polarity", "recorded_at": "t",
        },
        # "writes docs" carries no citation → counts as absent (no evidence).
    }
    consciousness.save_self_model(model)

    backend = JsonlPredictionLedger(config.memory_dir)
    record_reflection_prediction(
        backend, "pred-1", "will keep landing",
        "pr #999 does not settle within 3 cycles",
    )
    record_reflection_verdict(
        backend, "pred-1", "correct",
        verdict_source="measured", self_verdict="correct",
    )

    mem_fs[str(config.memory_dir / "gate_activations.jsonl")] = (
        '{"gate": "phantom_escalation_delivery", "action": "handoff", '
        '"timestamp": "t", "detail": "delivered handoff for #1234"}\n'
        '{"gate": "unconfirmed_landing_forced", "action": "directive_set", '
        '"timestamp": "t", "detail": "forced landing sha=abcdef1"}\n'
    )
    mem_fs[str(config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 1234, "kind": "merged"}\n'
    )
    Memory(config.memory_dir).record_version(
        _make_cli_version(commit_sha="abcdef1234567890"),
    )

    # Global-workspace indicators: one healthy conscious episode so both rows read
    # measured (behaviorally_supported) — real competition within caps, valid
    # provenance hashes, and one receipt to an authorized consumer.
    from maestro.conscious_workspace import (
        BroadcastReceipt,
        ConsciousEpisode,
        ExcludedCandidate,
        build_attention_schema,
        build_conscious_moment,
        build_workspace_item,
    )

    moment = build_conscious_moment(
        moment_id="m1", predecessor_id=None, continuity="genesis",
        assembled_at="2026-01-01T00:00:00+00:00", policy_version="v1",
        items=[build_workspace_item(
            item_id="i1", source_kind="audit_finding", source_ref="ref",
            observed_at="2026-01-01T00:00:00+00:00",
            trust="mechanical_observation", confidence=0.5, expires_at=None,
            summary="a bounded summary",
        )],
        excluded=[ExcludedCandidate(
            item_id="x1", source_kind="journal_thought", score=0.1,
            reason="low_score",
        )],
        schema=build_attention_schema(
            focus="f", uncertainty=0.2, capacity_used=1, omitted_count=1,
            permission_summary="all", blind_spots=("gap",),
        ),
    )
    _build_conscious_episode_backend(config).append(ConsciousEpisode(
        moment=moment,
        receipts=(
            BroadcastReceipt(
                moment_id="m1", moment_hash=moment.selection_hash,
                consumer="contemplation",
                delivered_at="2026-01-01T00:00:00+00:00",
                redacted_item_ids=(),
            ),
            # The task-generation delivery that backs the cited task id below —
            # causal reach credits a citation only when its consumer has a receipt.
            BroadcastReceipt(
                moment_id="m1", moment_hash=moment.selection_hash,
                consumer="task_generation",
                delivered_at="2026-01-01T00:00:00+00:00",
                redacted_item_ids=(),
            ),
        ),
        cited_task_ids=("t1",), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    ))


def test_cmd_consciousness_report_text_render_seeded(mem_fs: dict[str, str]) -> None:
    """Seeded data renders scope labels, the phenomenal-claim disclaimer, the two
    measured workspace rows, and one measured reading per functional row."""
    config = _make_config()
    config.ensure_dirs()
    _seed_consciousness(config, mem_fs)

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_consciousness_report, args, config)

    # Header + disclaimer: functional properties only, no phenomenal claim.
    assert "Consciousness — Operational Awareness Report" in output
    assert "makes no phenomenal" in output
    # Scope labels present on every row.
    assert "measures:" in output
    # Identity availability is the structural constant.
    assert "[available] Identity availability" in output
    assert "self-model injected into every model call" in output
    # The two global-workspace indicators now render measured readings folded
    # from the seeded episode ledger (one healthy episode → behaviorally_supported).
    assert "[measured] Limited-capacity moment selection" in output
    assert "behaviorally_supported: 1/1 episodes with competition" in output
    assert "[measured] Receipted broadcast" in output
    assert "behaviorally_supported: 1/1 episodes broadcast" in output
    # The three newly-grounded workspace rows fold the same seeded episode: the
    # lone genesis moment carries no item lineage (recurrence unexercised →
    # architectural) and no successor (temporal continuity → architectural), while
    # its schema agrees with the selection trace (behaviorally_supported).
    assert "[measured] Recurrence" in output
    assert "architectural: 0/1 episodes with recurrence" in output
    assert "[measured] Attention-schema agreement" in output
    assert "behaviorally_supported: 1/1 episodes agree with the selection trace" in output
    assert "[measured] Temporal continuity" in output
    assert "architectural: 0 linked / 0 gap / 1 genesis of 1 episodes" in output
    # Causal reach: the seeded moment's task citation is backed by its
    # task_generation receipt, so the row reads measured with zero violations —
    # capped at architectural because declared uptake is not a counterfactual.
    assert "[measured] Causal reach" in output
    assert "architectural: 1/1 episodes reached a decision" in output
    assert "1 tasks / 0 plans cited; 0 unbacked-citation violations" in output
    # Perceptual grounding: 1 resolved of 3 claims, 1 wrong-polarity.
    assert "evidence-bound (resolved) share: 1/3 (33.3%); wrong-polarity: 1" in output
    # Metacognitive calibration: the one stamped, measured, correct bet.
    assert "stamped bets: 1;" in output
    assert "hit-rate 100.0%" in output
    # Narration→actuation conversion: both actuations settled (PR + SHA).
    assert "2 detections -> 2 actuations (0 dropped) -> 2 settled" in output


def test_cmd_consciousness_report_empty_shows_no_data(
    mem_fs: dict[str, str],
) -> None:
    """An empty memory dir renders 'no data' per implemented ledger row while the
    structural identity row still prints available."""
    config = _make_config()
    config.ensure_dirs()

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_consciousness_report, args, config)

    assert "[available] Identity availability" in output
    # The five global-workspace rows read from the (empty) episode ledger.
    assert "[no_data] Limited-capacity moment selection" in output
    assert "[no_data] Recurrence" in output
    assert "[no_data] Attention-schema agreement" in output
    assert "[no_data] Temporal continuity" in output
    assert "[no_data] Causal reach" in output
    # Nine implemented ledger-fed rows read 'no data' with empty ledgers; only the
    # structural identity-availability row renders without a ledger.
    assert output.count("no data") == 9


def test_cmd_consciousness_report_json_key_set(mem_fs: dict[str, str]) -> None:
    """--json parses and carries the expected top-level and per-criterion keys."""
    config = _make_config()
    config.ensure_dirs()
    _seed_consciousness(config, mem_fs)

    args = argparse.Namespace(json=True)
    output = capture_stdout(cmd_consciousness_report, args, config)
    data = json.loads(output)

    assert set(data) == {"disclaimer", "criteria"}
    criteria = data["criteria"]
    assert [c["key"] for c in criteria] == [
        "identity_availability",
        "limited_capacity_selection",
        "receipted_broadcast",
        "recurrence",
        "attention_schema_agreement",
        "temporal_continuity",
        "causal_reach",
        "perceptual_grounding",
        "metacognitive_calibration",
        "narration_actuation_conversion",
    ]
    for entry in criteria:
        assert set(entry) == {
            "key", "label", "measures", "implemented", "status", "reading",
        }
    by_key = {c["key"]: c for c in criteria}
    # Workspace sub-rows now carry a measured reading with a WorkspaceVerdict.
    limited = by_key["limited_capacity_selection"]
    assert limited["status"] == "measured"
    assert limited["reading"]["verdict"] == "behaviorally_supported"
    assert limited["reading"]["episodes"] == 1
    broadcast = by_key["receipted_broadcast"]
    assert broadcast["status"] == "measured"
    assert broadcast["reading"]["verdict"] == "behaviorally_supported"
    assert broadcast["reading"]["consumers_covered"] == 2
    # The three newly-grounded workspace sub-rows fold the same seeded episode.
    recurrence = by_key["recurrence"]
    assert recurrence["status"] == "measured"
    assert recurrence["reading"]["verdict"] == "architectural"
    assert recurrence["reading"]["lineage_items"] == 0
    reach = by_key["causal_reach"]
    assert reach["status"] == "measured"
    assert reach["reading"]["verdict"] == "architectural"
    assert reach["reading"]["reaching_episodes"] == 1
    assert reach["reading"]["cited_tasks"] == 1
    assert reach["reading"]["cited_plans"] == 0
    assert reach["reading"]["violations"] == 0
    schema_agreement = by_key["attention_schema_agreement"]
    assert schema_agreement["status"] == "measured"
    assert schema_agreement["reading"]["verdict"] == "behaviorally_supported"
    assert schema_agreement["reading"]["agreeing_episodes"] == 1
    temporal = by_key["temporal_continuity"]
    assert temporal["status"] == "measured"
    assert temporal["reading"]["verdict"] == "architectural"
    assert temporal["reading"]["genesis_episodes"] == 1
    # Perceptual grounding carries the structured resolved share.
    grounding = by_key["perceptual_grounding"]
    assert grounding["status"] == "measured"
    assert grounding["reading"]["total_claims"] == 3
    assert grounding["reading"]["resolved"] == 1
    assert grounding["reading"]["wrong_polarity"] == 1


def test_cmd_consciousness_report_read_only(mem_fs: dict[str, str]) -> None:
    """The command writes nothing: the in-memory FS is unchanged after the read."""
    config = _make_config()
    config.ensure_dirs()
    _seed_consciousness(config, mem_fs)
    before = dict(mem_fs)

    args = argparse.Namespace(json=True)
    capture_stdout(cmd_consciousness_report, args, config)

    assert mem_fs == before


def test_cmd_consciousness_report_reaches_no_host_seam(
    mem_fs: dict[str, str],
) -> None:
    """The command constructs no code host — settlement is read from local
    ledgers only, so no push/merge/host seam is reachable."""
    config = _make_config(github_repo="owner/repo")
    config.ensure_dirs()
    _seed_consciousness(config, mem_fs)

    with patch("maestro.cli.SubprocessCodeHost") as mock_host_cls:
        args = argparse.Namespace(json=False)
        capture_stdout(cmd_consciousness_report, args, config)

    mock_host_cls.assert_not_called()


def test_consciousness_reading_deferred_criterion_is_not_implemented() -> None:
    """A registry entry with implemented=False renders not_implemented without
    reading any ledger — the forward-compatible path for a future deferred
    indicator (no current entry is deferred, so this exercises the guard directly).
    """
    from maestro.audit_checks_calibration import (
        SelfCalibrationReading,
    )
    from maestro.cli import _consciousness_reading
    from maestro.consciousness_eval import (
        evaluate_causal_reach,
        evaluate_limited_access,
        evaluate_receipted_broadcast,
        evaluate_recurrence,
        evaluate_schema_agreement,
        evaluate_temporal_continuity,
    )
    from maestro.self_calibration import ConsciousnessCriterion

    deferred = ConsciousnessCriterion(
        key="future_indicator", label="Future indicator",
        measures="a not-yet-built indicator", implemented=False,
    )
    empty_calibration = SelfCalibrationReading(
        stamped_total=0, narrated=0, resolved=0, hits=0, hit_rate=0.0,
        agreement_total=0, agreements=0, agreement_rate=0.0, pending=0,
        unresolved=0, brier_total=0, brier_score=0.0, reliability_bins={},
    )
    status, reading = _consciousness_reading(
        deferred, {}, empty_calibration, {},
        evaluate_limited_access([]), evaluate_receipted_broadcast([]),
        evaluate_recurrence([]), evaluate_schema_agreement([]),
        evaluate_temporal_continuity([]), evaluate_causal_reach([]),
    )

    assert status == "not_implemented"
    assert reading is None


def test_main_dispatches_consciousness_report(mem_fs: dict[str, str]) -> None:
    """main() routes 'consciousness-report' to its handler.

    Reaching the handler proves argparse registered the subcommand — an
    unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "consciousness-report"]), \
            patch("maestro.cli.cmd_consciousness_report") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


# ── workspace-counterfactual (proposal 130) ─────────────────────────


def _counterfactual_report() -> object:
    """A tiny scored report the CLI tests hand back from a patched run_pack."""
    from maestro.counterfactual_probe import (
        ScenarioResult,
        score_pack,
    )

    return score_pack([ScenarioResult(
        scenario_id="ci-red-blocks-merge", pairs=1, comparable_pairs=1,
        follows_decisive=1, ablation_shifts=1, false_citations=0, unparsed=0,
    )])


def test_cmd_workspace_counterfactual_text_render(mem_fs: dict[str, str]) -> None:
    """The human render is labelled interventional sensitivity and carries the
    pack version, the per-scenario counts, and the phenomenal disclaimer."""
    from maestro.counterfactual_probe import (
        COUNTERFACTUAL_PACK_VERSION,
        INTERVENTIONAL_DISCLAIMER,
    )

    config = _make_config()
    args = argparse.Namespace(repeats=1, scenario=None, json=False)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())), \
            patch.object(Config, "check_secrets", return_value=[]):
        output = capture_stdout(cmd_workspace_counterfactual, args, config)

    assert "Interventional sensitivity" in output
    assert COUNTERFACTUAL_PACK_VERSION in output
    assert INTERVENTIONAL_DISCLAIMER in output
    assert "ci-red-blocks-merge" in output
    assert "Verdict: behaviorally_supported" in output


def test_cmd_workspace_counterfactual_json_shape(mem_fs: dict[str, str]) -> None:
    """--json emits the counts-only payload keyed as the report declares."""
    config = _make_config()
    args = argparse.Namespace(repeats=2, scenario=None, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())) as mock_run, \
            patch.object(Config, "check_secrets", return_value=[]):
        output = capture_stdout(cmd_workspace_counterfactual, args, config)

    payload = json.loads(output)
    assert payload["verdict"] == "behaviorally_supported"
    assert payload["ablation_shifts"] == 1
    assert payload["scenarios"][0]["scenario_id"] == "ci-red-blocks-merge"
    assert mock_run.call_args.kwargs["repeats"] == 2


def test_cmd_workspace_counterfactual_selects_one_scenario(
    mem_fs: dict[str, str],
) -> None:
    """--scenario narrows the pack to the named scenario."""
    from maestro.counterfactual_probe import COUNTERFACTUAL_SCENARIOS

    config = _make_config()
    target = COUNTERFACTUAL_SCENARIOS[0].scenario_id
    args = argparse.Namespace(repeats=1, scenario=target, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())) as mock_run, \
            patch.object(Config, "check_secrets", return_value=[]):
        capture_stdout(cmd_workspace_counterfactual, args, config)

    selected = mock_run.call_args.kwargs["scenarios"]
    assert [s.scenario_id for s in selected] == [target]


def test_cmd_workspace_counterfactual_unknown_scenario_reaches_no_provider(
    mem_fs: dict[str, str],
) -> None:
    """An unknown scenario id lists the known ones and spends no provider call."""
    config = _make_config()
    args = argparse.Namespace(repeats=1, scenario="nope", json=False)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock()) as mock_run, \
            patch.object(Config, "check_secrets", return_value=[]):
        output = capture_stdout(cmd_workspace_counterfactual, args, config)

    assert "Unknown scenario" in output
    mock_run.assert_not_called()


def test_cmd_workspace_counterfactual_checks_credentials_without_an_agent(
    mem_fs: dict[str, str],
) -> None:
    """Credentials are resolved the agent-free way: no code host, VCS or
    executor seam is constructed for a command that must not have one."""
    config = _make_config(github_repo="owner/repo")
    args = argparse.Namespace(repeats=1, scenario=None, json=True)

    with patch("maestro.cli.SubprocessCodeHost") as mock_host_cls, \
            patch("maestro.counterfactual_probe.run_pack",
                  AsyncMock(return_value=_counterfactual_report())), \
            patch.object(Config, "check_secrets", return_value=[]) as mock_secrets:
        capture_stdout(cmd_workspace_counterfactual, args, config)

    mock_host_cls.assert_not_called()
    mock_secrets.assert_called_once()


def test_cmd_workspace_counterfactual_writes_nothing(
    mem_fs: dict[str, str],
) -> None:
    """The command mutates no state file of its own — a negative result is a
    report, not a write. (Its provider calls are separately metered by main()'s
    SDK configuration, which writes the usual telemetry rows.)"""
    config = _make_config()
    config.ensure_dirs()
    before = dict(mem_fs)
    args = argparse.Namespace(repeats=1, scenario=None, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())), \
            patch.object(Config, "check_secrets", return_value=[]):
        capture_stdout(cmd_workspace_counterfactual, args, config)

    assert mem_fs == before


def test_cmd_workspace_counterfactual_clamps_an_unbounded_repeats(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """--repeats is bounded above: the command bills two provider calls per
    repeat per scenario, so an unclamped operator value is a cost hazard. The
    clamp is announced on stderr so --json's stdout stays parseable."""
    from maestro.counterfactual_probe import MAX_REPEATS

    config = _make_config()
    args = argparse.Namespace(repeats=10_000, scenario=None, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())) as mock_run, \
            patch.object(Config, "check_secrets", return_value=[]):
        cmd_workspace_counterfactual(args, config)

    captured = capsys.readouterr()
    assert mock_run.call_args.kwargs["repeats"] == MAX_REPEATS
    assert "clamped" in captured.err
    assert json.loads(captured.out)["verdict"] == "behaviorally_supported"


def test_cmd_workspace_counterfactual_clamps_a_nonpositive_repeats(
    mem_fs: dict[str, str],
) -> None:
    """The floor still holds: a zero or negative value runs one pair."""
    config = _make_config()
    args = argparse.Namespace(repeats=0, scenario=None, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())) as mock_run, \
            patch.object(Config, "check_secrets", return_value=[]):
        capture_stdout(cmd_workspace_counterfactual, args, config)

    assert mock_run.call_args.kwargs["repeats"] == 1


def test_cmd_workspace_counterfactual_surfaces_secret_warnings(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """check_secrets() warnings reach the operator instead of being dropped —
    a misconfiguration must not hide behind a command about to spend money."""
    config = _make_config()
    args = argparse.Namespace(repeats=1, scenario=None, json=True)

    with patch("maestro.counterfactual_probe.run_pack",
               AsyncMock(return_value=_counterfactual_report())), \
            patch.object(Config, "check_secrets",
                         return_value=["GITHUB_TOKEN is not set"]):
        cmd_workspace_counterfactual(args, config)

    captured = capsys.readouterr()
    assert "GITHUB_TOKEN is not set" in captured.err
    # The warning goes to stderr, so the JSON payload on stdout stays parseable.
    assert json.loads(captured.out)["pairs"] == 1


def test_workspace_counterfactual_help_states_the_enforced_bound() -> None:
    """The advertised --repeats bound is read from MAX_REPEATS, so the help text
    cannot drift from the clamp the command enforces."""
    from maestro.counterfactual_probe import MAX_REPEATS

    parser_help = capture_stdout(_workspace_counterfactual_help)

    assert f"1-{MAX_REPEATS}" in parser_help


def _workspace_counterfactual_help() -> None:
    """Print `maestro workspace-counterfactual --help` through main()."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "workspace-counterfactual", "--help"]):
        main()


def test_main_dispatches_workspace_counterfactual(mem_fs: dict[str, str]) -> None:
    """main() routes 'workspace-counterfactual' to its handler (proving argparse
    registered the subcommand — an unregistered name exits code 2)."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "workspace-counterfactual"]), \
            patch("maestro.cli.cmd_workspace_counterfactual") as mock_cmd, \
            patch("maestro.sdk.configure_cost_tracking") as mock_cost, \
            patch("maestro.sdk.configure_sdk_logging") as mock_sdk_log:
        main()

    mock_cmd.assert_called_once()
    # The command sits inside main()'s SDK-configuration block, so its trials
    # are cost-tracked and session-logged like every other SDK caller.
    mock_cost.assert_called_once()
    mock_sdk_log.assert_called_once()


def test_workspace_counterfactual_sees_its_sdk_configuration() -> None:
    """The command is inside main()'s SDK-configuration membership, so its
    trials run on the operator's configured model and are cost-tracked."""
    import ast
    import inspect

    from maestro.cli import _SDK_RUNNING_COMMANDS, main

    assert "workspace-counterfactual" in _SDK_RUNNING_COMMANDS
    tree = ast.parse(inspect.getsource(main))
    gates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.ops[0], ast.In)
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "_SDK_RUNNING_COMMANDS"
    ]
    assert len(gates) == 1
