"""Cut-over flag collapse guards — the retirement half of the config-flag lifecycle.

``maestro/flag_lifecycle.py`` carries the *front* half of the flag lifecycle: an
``EXPERIMENTAL_FLAGS`` manifest of default-off experiments with a ``review_by``
horizon (``tests/test_config_flag_lifecycle.py``). This module guards the
*retirement* half — the ``CUTOVER_FLAGS`` manifest of default-on cut-over flags,
each declaring the ``off_path_symbol`` its flag-off branch lives behind and a
lifecycle ``stage``. The guards keep the manifest structurally honest and force a
half-done collapse to red:

* every entry names a real ``Config`` field and a valid ``stage`` / ``review_by``;
* ``find_stage_violations`` binds each stage to whether its off-path symbol still
  exists on the real tree, so a completed collapse must drop its entry rather than
  leaving a marker, and a declared-but-unfinished collapse reds the build;
* a literal growth budget keeps the manifest from becoming its own un-pruned
  ratchet (the ``_MAX_STRUCTURAL_GUARDS`` idiom from
  ``tests/test_structural_guards.py``).

Like ``tests/test_config_flag_lifecycle.py`` this reads the real tree on disk (no
``mem_fs``, no network, no subprocess), reusing
``tests.support.structural_guard.symbols_defined_in`` for the whole-``maestro/``
symbol scan. CI-only: no runtime behaviour, no state file, no trust-chain change.
"""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from maestro.config import Config
from maestro.config_settings import _SECTION_MODELS
from maestro.flag_lifecycle import (
    CUTOVER_FLAGS,
    FREEZE_DATE,
    CutoverFlag,
    cutover_burn_in_report,
    find_stage_violations,
    parse_review_by,
)
from tests.support.structural_guard import symbols_defined_in

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"

#: ``(section, TOML key) -> Config field name``, derived from the declarative
#: section models (``Field(alias=...)`` or the field name) — the post-proposal-106
#: replacement for the retired ``_TOML_TO_CONFIG`` mapping, keyed the same way so
#: :func:`default_on_boolean_fields` stays keyed on ``Config`` field names.
_SECTION_MODEL_MAPPING: dict[tuple[str, str], str] = {
    (section, field.alias or name): name
    for section, model_cls in _SECTION_MODELS.items()
    for name, field in model_cls.model_fields.items()
}

#: The committed ``maestro.toml`` — the real default surface ("Config field
#: defaults are for tests only") the registry-completeness guard parses, the
#: same file the sibling ``tests/test_config_flag_lifecycle.py`` symmetry guard
#: reads.
_MAESTRO_TOML = Path(__file__).resolve().parent.parent / "maestro.toml"

#: The four valid lifecycle stages a :class:`CutoverFlag` may carry — the same set
#: the field's ``Literal`` annotation declares, restated so a runtime-constructed
#: entry (a manifest hand-edit) that slips past the annotation is caught.
_VALID_STAGES = {"cutover", "collapse_eligible", "collapsed", "retain"}

#: Growth budget: equal to today's ten entries. Proposal 113 added
#: ``stage_progression_enabled`` as a genuine new cut-over flag retiring the
#: priced stage-readiness verdict at collapse, the proposal-099 graduation
#: (2026-08-23) added ``base_advance_re_entry_enabled`` /
#: ``unwinnable_race_escalation_enabled`` as a pair, the proposal-089
#: graduation (2026-08-25) added ``observed_ready_standdown_enabled``, the
#: 2026-08-26 graduation added ``owned_committed_push_actuator_enabled``, and the
#: proposal-122 graduation (2026-09-03) added ``account_ui_v2`` — the
#: account-dashboard navigation shell promoted from a default-off dark launch,
#: keeping the flat-nav off-branch as a one-line rollback through its
#: observation window — and the proposal-126 graduation (2026-09-08) added
#: ``gate_lease_enabled``, the gate execution lease promoted from a default-off
#: experiment to fix the #3760 host-sized-worker OOM, keeping the auto-width
#: off-branch as a retained rollback, and the proposal-128 increment
#: (2026-09-09) added ``tend_main_merge_enabled`` — the merge-queue cutover
#: kill-switch defaulted on, keeping the eager ``origin/main`` merge-and-push in
#: ``_tend_single_pr`` as the live off-branch through the queue observation
#: window — and the proposal-130 step 11 flip (2026-09-30) added
#: ``conscious_workspace_observe_enabled``, the observe-only attend phase turned
#: on everywhere for its blind burn-in, flag-off the rollback — and the
#: proposal-116 graduation (2026-10-07) added ``generation_admission_enabled``
#: and ``retire_unlanded_limitations_enabled``, the plan-mill closure levers
#: promoted from prod-only canaries to the base default — while proposal 111 collapsed
#: ``live_work_corpus_enabled`` and dropped its entry and proposal 117 collapsed
#: the proposal-099 pair to unconditional behaviour and dropped this cap back by
#: two, and proposal 149 deleted the purpose engine and dropped
#: ``purpose_engine_enabled``, then deleted the stage machinery and dropped
#: ``stage_progression_enabled`` — each change paired with the matching cap
#: adjustment in the same reviewed diff. A literal integer (not ``len(CUTOVER_FLAGS)``) pinned at the current
#: population so adding an eleventh cut-over flag without collapsing an aged one reds
#: the build — the addition-beats-deletion signal the bound exists to catch,
#: mirroring ``_MAX_STRUCTURAL_GUARDS == len(CHOKEPOINT_GUARDS)`` in
#: ``tests/test_structural_guards.py``. A cap one slot above the population would
#: leave a free slot and defeat the zero-sum invariant it claims to enforce.
_MAX_CUTOVER_FLAGS = 10

#: The single ``Literal`` type reused by the synthetic-entry factory below.
_CutoverStage = Literal["cutover", "collapse_eligible", "collapsed", "retain"]


def _config_field_names() -> set[str]:
    """Every declared ``Config`` dataclass field name (same idiom as the sibling
    ``tests/test_config_flag_lifecycle.py`` guard)."""
    return {f.name for f in dataclasses.fields(Config)}


def _within_budget(count: int) -> bool:
    """True while the manifest holds no more than :data:`_MAX_CUTOVER_FLAGS`."""
    return count <= _MAX_CUTOVER_FLAGS


def _synthetic_flag(flag: str, stage: _CutoverStage, symbol: str) -> CutoverFlag:
    """A well-formed in-memory :class:`CutoverFlag` for the negative controls."""
    return CutoverFlag(
        flag=flag,
        proposal=1,
        cutover_cycle="2026-01-01",
        off_path_symbol=symbol,
        review_by="2027-01-01",
        stage=stage,
        note="synthetic",
    )


def test_manifest_is_non_empty() -> None:
    """Vacuous-pass guard: an empty manifest would let the scans below pass without
    validating anything (mirrors the sibling lifecycle guards' non-emptiness
    checks)."""
    assert CUTOVER_FLAGS, "Expected a non-empty cut-over-flag manifest"


def test_every_cutover_flag_names_a_real_config_field() -> None:
    """Every manifest ``flag`` names a declared ``Config`` field.

    The manifest keys on the ``Config`` field name; a misspelled or since-deleted
    field would otherwise surface only when someone read a later burn-in report and
    found a phantom row. Mirrors the experimental-flag equivalent. The seeded
    negative control keeps the clean-tree half from passing vacuously — a phantom
    name must surface on the wrong side of the same set difference.
    """
    config_fields = _config_field_names()
    assert config_fields, "Expected declared Config dataclass fields"
    orphans = sorted({e.flag for e in CUTOVER_FLAGS} - config_fields)
    assert orphans == [], (
        "These CUTOVER_FLAGS entries name fields not declared on Config: "
        f"{orphans}. Fix the manifest's flag name, or add the Config field."
    )
    # Seeded negative control: a phantom flag name is reported by the same diff.
    phantom = "definitely_not_a_config_field"
    assert phantom not in config_fields
    assert sorted({phantom} - config_fields) == [phantom]


def test_every_stage_is_valid_and_review_by_parses() -> None:
    """Every ``stage`` is one of the four valid values and every ``review_by``
    parses via ``parse_review_by``.

    A bad stage (a manifest hand-edit that bypasses the ``Literal`` at runtime)
    would let ``find_stage_violations`` silently skip the entry; a malformed
    ``review_by`` would break the later burn-in report. Both are typo tripwires.
    """
    for entry in CUTOVER_FLAGS:
        assert entry.stage in _VALID_STAGES, (
            f"{entry.flag}: stage {entry.stage!r} is not one of {_VALID_STAGES}"
        )
        parsed = parse_review_by(entry.review_by)
        assert isinstance(parsed, date)


def test_manifest_stage_consistency() -> None:
    """No manifest entry breaks the stage<->off-path invariant on the real tree.

    Reads every symbol defined under ``maestro/`` and asserts
    ``find_stage_violations`` is empty — a ``cutover``/``retain``/``collapse_eligible``
    entry whose off-path was deleted, a ``collapse_eligible`` entry whose off-path is
    still wired, or any lingering ``collapsed`` entry all red the build here. The
    non-empty ``defined`` guard keeps a broken scan (empty set) from passing this
    vacuously.
    """
    defined = symbols_defined_in(_MAESTRO_ROOT)
    assert defined, f"Expected defined symbols under {_MAESTRO_ROOT}"
    violations = find_stage_violations(CUTOVER_FLAGS, defined)
    assert violations == [], (
        "Cut-over manifest entries are out of step with the tree:\n"
        + "\n".join(violations)
    )


def test_stage_violations_negative_control() -> None:
    """Synthetic control: ``find_stage_violations`` flags every inconsistent
    combination and passes every consistent one, over in-memory ``CutoverFlag``s
    and a synthetic symbol set — no filesystem.

    Without this, a broken helper (flagging nothing, or everything) would make the
    clean-tree assertion above pass vacuously. Exercises every branch:
    ``cutover``+absent, ``retain``+absent, ``collapse_eligible``+absent,
    ``collapse_eligible``+present, ``collapsed``+present, and ``collapsed``+absent are
    all flagged; only ``cutover``+present and ``retain``+present are clean. A
    ``collapse_eligible`` entry whose off-path was already deleted is a completed
    collapse that left its manifest marker behind — the same leftover-entry mistake
    the retired-off-path rule catches for ``cutover``/``retain``, so it must also red.
    Each flagged entry is asserted against the *reason* its message names (not merely
    the flagged flag set) so a helper that fires the wrong rule for the right flag is
    still caught.
    """
    defined = {"present_symbol"}
    flagged = [
        _synthetic_flag("cutover_absent", "cutover", "gone_symbol"),
        _synthetic_flag("retain_absent", "retain", "gone_symbol"),
        _synthetic_flag("eligible_absent", "collapse_eligible", "gone_symbol"),
        _synthetic_flag("eligible_present", "collapse_eligible", "present_symbol"),
        _synthetic_flag("collapsed_present", "collapsed", "present_symbol"),
        _synthetic_flag("collapsed_absent", "collapsed", "gone_symbol"),
    ]
    clean = [
        _synthetic_flag("cutover_present", "cutover", "present_symbol"),
        _synthetic_flag("retain_present", "retain", "present_symbol"),
    ]
    violations = find_stage_violations(flagged + clean, defined)
    by_flag = {message.split(":", 1)[0]: message for message in violations}
    assert set(by_flag) == {
        "cutover_absent",
        "retain_absent",
        "eligible_absent",
        "eligible_present",
        "collapsed_present",
        "collapsed_absent",
    }
    # Intent-lock each violation to the specific rule that fired, so a helper that
    # reports the wrong reason for the right flag no longer passes vacuously. The
    # retired-off-path rule fires for ``cutover``/``retain``/``collapse_eligible``+absent.
    assert "no longer defined under maestro/" in by_flag["cutover_absent"]
    assert "no longer defined under maestro/" in by_flag["retain_absent"]
    assert "no longer defined under maestro/" in by_flag["eligible_absent"]
    assert "still defined under maestro/" in by_flag["eligible_present"]
    assert "still in the manifest" in by_flag["collapsed_present"]
    assert "still in the manifest" in by_flag["collapsed_absent"]


def test_registry_within_budget() -> None:
    """The manifest fits its growth budget.

    A single hard cap makes adding a cut-over flag a zero-sum decision — collapse an
    aged entry, or raise ``_MAX_CUTOVER_FLAGS`` in the same reviewed diff and say
    why. A cap bump not paired with a compensating collapse is the
    addition-beats-deletion signal this budget exists to catch.
    """
    count = len(CUTOVER_FLAGS)
    assert _within_budget(count), (
        f"manifest now holds {count} entries against the cap of "
        f"{_MAX_CUTOVER_FLAGS}: collapse an entry, or raise _MAX_CUTOVER_FLAGS in "
        "the same reviewed diff and justify it"
    )


def test_budget_bites_past_cap() -> None:
    """The budget predicate is a real numeric bound, not a vacuous ``True`` —
    accepts the boundary count and rejects one past the cap, exercising both
    branches of the predicate (mirrors ``test_population_budget_bites_past_the_cap``
    in ``tests/test_structural_guards.py``)."""
    assert _within_budget(_MAX_CUTOVER_FLAGS) is True
    assert _within_budget(_MAX_CUTOVER_FLAGS + 1) is False


def test_budget_cap_equals_current_manifest_size() -> None:
    """The cap sits exactly at the current manifest size, not one slot above it.

    This is the zero-sum invariant the growth budget exists to enforce: with the cap
    pinned to the population, the *next* cut-over flag added without collapsing an
    aged one trips ``test_registry_within_budget`` — mirroring
    ``_MAX_STRUCTURAL_GUARDS == len(CHOKEPOINT_GUARDS)`` in
    ``tests/test_structural_guards.py``. A cap left one slot above the population
    would silently absorb a fourth flag, and ``test_budget_bites_past_cap`` — which
    checks only behaviour *relative to* the constant — cannot surface that off-by-one.
    """
    assert len(CUTOVER_FLAGS) == _MAX_CUTOVER_FLAGS, (
        f"cap {_MAX_CUTOVER_FLAGS} must equal the manifest size "
        f"{len(CUTOVER_FLAGS)} so the next unpaired addition reds the build; a cap "
        "above the population leaves a free slot and defeats the zero-sum budget"
    )


# ---------------------------------------------------------------------------
# Registry-completeness guard (proposal 111 step 2)
# ---------------------------------------------------------------------------
# ``find_stage_violations`` above keeps the entries the manifest *already holds*
# honest, but nothing forces a newly-introduced default-on cut-over flag to be
# entered at all: it has readers (the dead-flag reader guard passes) and is not
# an experiment (it never joins ``EXPERIMENTAL_FLAGS``), so it falls through
# every existing guard and the manifest silently drifts incomplete. This guard
# closes that gap by partitioning every default-on boolean flag in the committed
# ``maestro.toml`` into exactly one of two documented buckets — a cut-over flag
# registered in ``CUTOVER_FLAGS``, or a feature master-switch recorded in the
# allowlist below — and failing on any flag that lands in neither. Adding a
# default-on flag then becomes a conscious classification decision. Pure over an
# already-parsed TOML dict and the two code constants; the allowlist, population
# helper, and detector mirror ``_KNOWN_READERLESS_FIELDS`` /
# ``find_readerless_fields`` in ``tests/test_config_flag_lifecycle.py``.


#: Every default-on boolean ``Config`` field whose flag-*off* disables a
#: *feature* (not live old logic) — the master-switch / escape-hatch half of the
#: default-on surface, the complement of :data:`CUTOVER_FLAGS`. Each maps to a
#: one-line reason (mirroring ``_KNOWN_READERLESS_FIELDS``), keyed on the
#: ``Config`` field name so it honours the section-model alias renames (e.g. TOML
#: ``[artifacts] review_enabled`` -> ``artifact_review_enabled``) and stays
#: directly comparable to ``CUTOVER_FLAGS`` and the default-on population.
#: :func:`test_master_switch_allowlist_is_current` keeps it honest: a promoted,
#: deleted, or reclassified flag turns its stale entry red.
_MASTER_SWITCH_FLAGS: dict[str, str] = {
    "artifact_review_enabled": (
        "master switch for the ArtifactReviewer bundle-classification phase; "
        "flag-off disables artifact review"
    ),
    "audit_impact_review_enabled": (
        "master switch for audit rule 9 (plan impact review); flag-off disables "
        "the rule"
    ),
    "audit_stale_entry_enabled": (
        "master switch for audit rule 6 (stale self-model entry); flag-off "
        "disables the rule"
    ),
    "branch_promotion_pressure_enabled": (
        "master switch for the branch-promotion-pressure subsystem (proposal 116); "
        "flag-off silences audit rule 7, the two task-gate directive bridges, the "
        "three task-generation promotion hints, the reflect() promotable-backlog "
        "line, the kept_task_branches_v1 seed mirror, and the step-5e "
        "merge-clean auto-direction"
    ),
    "branch_resolved_gate_enabled": (
        "master switch for the branch-resolved remediation dequeue gate; flag-off "
        "disables the gate"
    ),
    "ci_pending_hold_dequeue_enabled": (
        "gates the pre-pop CI-pending hold that leaves an owned CI-pending-only PR "
        "queued; flag-off disables the hold"
    ),
    "compound_scope_gate_enabled": (
        "master switch for the compound-scope plan gate; flag-off disables the gate"
    ),
    "criteria_alignment_enabled": (
        "master switch for the acceptance-criteria alignment outcome check; "
        "flag-off disables it"
    ),
    "gate_lease_admission_cap_enabled": (
        "gates the gate lease's admission cap (slots = cores // 4, a 600s queue "
        "wait); flag-off restores slots == cores and the 30s fail-open wait"
    ),
    "gate_strip_redirections_enabled": (
        "gates stripping shell redirections before the action gate reads a push "
        "refspec (proposal 096); flag-off disables stripping"
    ),
    "green_ready_handoff_enabled": (
        "master switch for audit rule 22 (green-ready handoff); flag-off disables "
        "the rule"
    ),
    "investigation_observable_gate_enabled": (
        "master switch for the investigation-observable post-call task-selection "
        "gate; flag-off disables the gate"
    ),
    "investigation_rate_enabled": (
        "master switch for the investigation-rate task-generation cadence; "
        "flag-off disables it"
    ),
    "investigation_streak_enabled": (
        "master switch for the investigation-streak task-generation cadence; "
        "flag-off disables it"
    ),
    "loop_pending_cluster_sweep_enabled": (
        "master switch for the pending-plan theme-cluster prune sweep; flag-off "
        "disables the sweep"
    ),
    "loop_restatement_sweep_enabled": (
        "master switch for the pending-plan restatement-chain prune sweep; "
        "flag-off disables the sweep"
    ),
    "merge_block_local_probe_enabled": (
        "master switch for the local merge-block probe; flag-off disables the probe"
    ),
    "orphaned_cure_enabled": (
        "master switch for audit rule 11 (orphaned-cure sweep); flag-off disables "
        "the rule"
    ),
    "orphaned_cure_retire_enabled": (
        "gates the rule-11 retire actuator; flag-off disables the retirement "
        "disposition"
    ),
    "owned_act_drought_enabled": (
        "master switch for audit rule 18 (owned-act drought); flag-off disables "
        "the rule"
    ),
    "peak_throttle_weekdays_only": (
        "restricts peak-throttle to Mon-Fri; flag-off applies peak-throttle every "
        "day (a feature toggle, not live old logic)"
    ),
    "phantom_escalation_enabled": (
        "master switch for audit rule 12 (phantom escalation); flag-off disables "
        "the rule"
    ),
    "post_implement_review": (
        "runs the quality gates after implement() before PR creation; flag-off "
        "skips the post-implement review"
    ),
    "pr_ready_for_review_notify_enabled": (
        "gates the Telegram ready-for-review ping; flag-off disables the "
        "notification"
    ),
    "prior_task_context_enabled": (
        "master switch for prior-task context injection into task generation; "
        "flag-off disables it"
    ),
    "promotable_backlog_directive_enabled": (
        "master switch for the promotable-backlog directive; flag-off disables it"
    ),
    "promotable_branch_hint_enabled": (
        "master switch for the promotable-branch hint in task generation; flag-off "
        "disables it"
    ),
    "publication_enabled": (
        "master switch for the publication approval surface and Publisher; "
        "flag-off disables publication"
    ),
    "publication_secret_scan_enabled": (
        "gates the pre-push hard-block secret rescan on full files; flag-off "
        "disables that scan (currently reader-less, in _KNOWN_READERLESS_FIELDS)"
    ),
    "quiet_skip_enabled": (
        "master switch for quiet-skip status-update dedup; flag-off disables it"
    ),
    "remediation_noop_gate_enabled": (
        "master switch for the remediation no-op dequeue gate; flag-off disables "
        "the gate"
    ),
    "resolve_addressed_threads_enabled": (
        "gates the loop's auto-resolution of addressed AI-finding threads; "
        "flag-off disables auto-resolution"
    ),
    "salvage_before_disengage_enabled": (
        "gates the pr_salvage oracle veto over stand-down/disengage (proposal "
        "094); flag-off disables the salvage gate"
    ),
    "security_headers_enabled": (
        "master switch for the dashboard SecurityHeadersMiddleware; flag-off "
        "disables the security response headers"
    ),
    "self_directed_tasks_enabled": (
        "master switch for the self-directed task pipeline (proposal 116); "
        "flag-off stops scheduling generate_task while external / "
        "PR-remediation work keeps running"
    ),
    "settlement_reconciler_enabled": (
        "master switch for the settlement reconciler / settled_verdicts.jsonl "
        "pre-filter (proposal 097); flag-off disables it"
    ),
    "settlement_record_reconciler_enabled": (
        "master switch for the on-settlement record-reconciler owner (proposal "
        "103); flag-off disables it"
    ),
    "task_branch_reuse_enabled": (
        "master switch for task-branch reuse as an evolution base via "
        "source_task_id (proposal 116); flag-off bases every evolution PR "
        "fresh-from-main"
    ),
    "tend_bug_comments_enabled": (
        "gates routing a remediation task for unresolved AI bug review comments; "
        "flag-off disables that routing"
    ),
    "tend_planless_prs_enabled": (
        "gates tending an open evolution PR after its plan leaves 'implemented'; "
        "flag-off disables planless tending"
    ),
    "tend_prs_enabled": (
        "master switch for the evolution-PR tending phase; flag-off disables PR "
        "tending entirely"
    ),
    "uncalled_symbol_gate_enabled": (
        "master switch for the uncalled-symbol self-review gate; flag-off disables "
        "the gate"
    ),
    "unconfirmed_landing_enabled": (
        "master switch for audit rule 14 (unconfirmed own landing); flag-off "
        "disables the rule"
    ),
    "user_pr_message_remediation_enabled": (
        "gates routing a user message naming an open own PR as full remediation; "
        "flag-off disables that routing"
    ),
    "user_request_blocks_standdown_enabled": (
        "gates refusing a stand-down while a pending user request names the PR; "
        "flag-off disables the block"
    ),
    "version_archive_enabled": (
        "master switch for the version_archive.jsonl VersionRecord append; "
        "flag-off disables version archiving"
    ),
    "wip_primacy_enabled": (
        "master switch for the WIP-primacy head-of-chain block gate (proposal "
        "092); flag-off disables WIP-primacy"
    ),
}


def default_on_boolean_fields(
    data: dict[str, Any],
    mapping: Mapping[tuple[str, str], str] = _SECTION_MODEL_MAPPING,
) -> set[str]:
    """Return the ``Config`` field names whose *data* TOML value is ``True``.

    Pure over an already-parsed TOML *data* dict — mirroring
    :func:`find_readerless_fields` / ``maestro_toml_scalar_pairs`` in the sibling
    lifecycle guard — so the real-tree guard and the negative controls share one
    definition of "default-on boolean". Iterates the declarative
    ``(section, key) -> Config field`` *mapping* (default
    :data:`_SECTION_MODEL_MAPPING`, derived from the section models) rather than
    re-walking the TOML with hand-rolled carve-outs, so the population is complete
    by construction and already keyed on ``Config`` field names — directly
    comparable to :data:`CUTOVER_FLAGS`. Uses ``value is True`` (not truthiness) so
    a non-bool scalar (a whole number like ``5``, a non-empty string) can never
    masquerade as a default-on flag. A section absent from *data* is skipped rather
    than raising.
    """
    on: set[str] = set()
    for (section, key), field in mapping.items():
        section_data = data.get(section)
        if isinstance(section_data, dict) and section_data.get(key) is True:
            on.add(field)
    return on


def find_unclassified_cutover_flags(
    default_on: set[str],
    registered: set[str],
    allowlisted: set[str],
) -> list[str]:
    """Return the sorted default-on fields in neither the manifest nor allowlist.

    Pure over its three sets (the exact shape of :func:`find_readerless_fields`),
    so the real-tree guard and the seeded negative control share one definition
    of "unclassified cut-over flag": a default-on boolean that is neither
    registered in :data:`CUTOVER_FLAGS` nor documented as a feature master-switch
    in :data:`_MASTER_SWITCH_FLAGS`.
    """
    return sorted(
        name
        for name in default_on
        if name not in registered and name not in allowlisted
    )


def test_every_default_on_flag_is_classified() -> None:
    """Every default-on boolean flag is classified into exactly one bucket.

    The registry-completeness guard: parse the committed ``maestro.toml``, build
    the default-on boolean population, and assert every field is either registered
    in :data:`CUTOVER_FLAGS` or documented in :data:`_MASTER_SWITCH_FLAGS`. A
    newly-added default-on flag that is neither — a genuine cut-over flag omitted
    from the manifest, or an undocumented master-switch — reds the build. The
    non-empty population guard keeps a broken parse (empty set) from passing this
    vacuously.
    """
    data = tomllib.loads(_MAESTRO_TOML.read_text(encoding="utf-8"))
    population = default_on_boolean_fields(data)
    assert population, f"Expected default-on boolean flags in {_MAESTRO_TOML}"
    unclassified = find_unclassified_cutover_flags(
        population,
        {entry.flag for entry in CUTOVER_FLAGS},
        set(_MASTER_SWITCH_FLAGS),
    )
    assert unclassified == [], (
        "These default-on boolean flags are classified in neither bucket: "
        f"{unclassified}. Either declare the flag's collapse plan (a CUTOVER_FLAGS "
        "entry with an off_path_symbol and a review_by), or document why it is a "
        "feature master-switch (a _MASTER_SWITCH_FLAGS entry with a reason)."
    )


def test_master_switch_allowlist_is_current() -> None:
    """Every allowlisted master-switch is still default-on and singly-classified.

    The self-cleaning tripwire (mirrors
    ``test_known_readerless_allowlist_is_current``): a promoted or deleted flag
    (no longer a default-on boolean in ``maestro.toml``) or a reclassified flag
    (now also registered in :data:`CUTOVER_FLAGS`) turns its stale allowlist entry
    red, so the allowlist never silently misrepresents the tree.
    """
    data = tomllib.loads(_MAESTRO_TOML.read_text(encoding="utf-8"))
    population = default_on_boolean_fields(data)
    assert population, f"Expected default-on boolean flags in {_MAESTRO_TOML}"
    registered = {entry.flag for entry in CUTOVER_FLAGS}
    for field in _MASTER_SWITCH_FLAGS:
        assert field in population, (
            f"Allowlisted master-switch flag {field!r} is no longer a default-on "
            "boolean in maestro.toml — remove its _MASTER_SWITCH_FLAGS entry."
        )
        assert field not in registered, (
            f"Master-switch flag {field!r} is also registered in CUTOVER_FLAGS — a "
            "flag must be classified once. Remove its _MASTER_SWITCH_FLAGS entry."
        )


def test_default_on_boolean_fields_matches_on_bool_not_truthiness() -> None:
    """Negative control: the population helper matches ``is True``, not truthiness.

    Over synthetic in-memory data and a synthetic mapping — no filesystem — only
    the field whose value is literally ``True`` is returned; a truthy int, a
    non-empty string, and a literal ``False`` are all excluded, so a non-bool
    scalar can never masquerade as a default-on flag. A mapping row whose section
    is absent from the data is skipped rather than raising. Without this, a helper
    that used truthiness would silently pull ints/strings into the population.
    """
    mapping: dict[tuple[str, str], str] = {
        ("loop", "real_flag"): "real_flag",
        ("loop", "int_flag"): "int_flag",
        ("loop", "str_flag"): "str_flag",
        ("loop", "off_flag"): "off_flag",
        ("absent", "missing"): "missing",
    }
    data: dict[str, Any] = {
        "loop": {
            "real_flag": True,
            "int_flag": 5,
            "str_flag": "x",
            "off_flag": False,
        },
    }
    assert default_on_boolean_fields(data, mapping) == {"real_flag"}


def test_classification_detects_seeded_unclassified_flag() -> None:
    """Negative control: the detector reports only the unclassified field.

    Over in-memory sets — no filesystem — a field in neither bucket is reported,
    while a registered field and an allowlisted field are not, exercising both
    sides of the membership test. Without this, a broken detector (reporting
    nothing, or everything) would make the real-tree assertion pass vacuously.
    """
    default_on = {"registered_flag", "allowlisted_flag", "unclassified_flag"}
    registered = {"registered_flag"}
    allowlisted = {"allowlisted_flag"}
    unclassified = find_unclassified_cutover_flags(
        default_on, registered, allowlisted,
    )
    assert unclassified == ["unclassified_flag"]


# ---------------------------------------------------------------------------
# Cut-over burn-in report (proposal 111 step 3)
# ---------------------------------------------------------------------------
# ``cutover_burn_in_report`` is the advisory evidence surface: it joins each
# ``cutover``-stage entry against a divergence fire-rate and — only when the flag
# is past its floor with a zero-divergence signal — recommends collapse. These
# tests mirror ``test_audit_flags_report_marks_overdue_row_and_joins_activations``
# in ``tests/test_config_flag_lifecycle.py``: synthetic fixtures, a *fixed* today,
# an in-memory ``activation_counts`` dict, assertions over ``{flag: row}``.


def _cutover_entry(
    flag: str,
    stage: _CutoverStage,
    review_by: str,
    *,
    gate: str | None = None,
    cutover_cycle: str = "2026-01-01",
) -> CutoverFlag:
    """A well-formed in-memory :class:`CutoverFlag` for the burn-in report tests.

    Distinct from :func:`_synthetic_flag` (which fixes ``review_by`` and carries no
    ``gate``): the burn-in report keys on ``review_by`` for its floor and on
    ``gate`` for its divergence measure, so both must vary per fixture.
    """
    return CutoverFlag(
        flag=flag,
        proposal=1,
        cutover_cycle=cutover_cycle,
        off_path_symbol="present_symbol",
        review_by=review_by,
        stage=stage,
        note="synthetic",
        gate=gate,
    )


def test_burn_in_report_recommendation_matrix() -> None:
    """The recommendation is ``past_floor and gate-measured zero divergence``.

    Over four ``cutover``-stage fixtures against one fixed ``today`` and an
    in-memory ``activation_counts``: a below-floor entry is never recommended; an
    aged entry whose divergence gate fired zero times is recommended; an aged entry
    with a nonzero divergence count is not; and an aged entry with no divergence
    gate (unmeasured, ``divergences is None``) is not — burn-in cannot be confirmed
    without evidence. The row shape (``age_days``, ``past_floor``, ``divergences``)
    is asserted alongside so a wrong join surfaces here, not in the CLI render.
    """
    today = date(2027, 2, 1)
    flags = (
        _cutover_entry(
            "below_floor", "cutover", "2027-06-30", gate="below_gate",
        ),
        _cutover_entry(
            "aged_clean", "cutover", "2027-01-01", gate="clean_gate",
            cutover_cycle="2027-01-01",
        ),
        _cutover_entry(
            "aged_noisy", "cutover", "2027-01-01", gate="noisy_gate",
        ),
        _cutover_entry(
            "aged_unmeasured", "cutover", "2027-01-01", gate=None,
        ),
    )
    rows = cutover_burn_in_report(flags, {"noisy_gate": 3}, today)
    by_flag = {row["flag"]: row for row in rows}

    # Below its floor: never recommended, even with a zero-divergence gate.
    assert by_flag["below_floor"]["past_floor"] is False
    assert by_flag["below_floor"]["divergences"] == 0
    assert by_flag["below_floor"]["recommend_collapse_eligible"] is False

    # Aged past floor + zero-divergence gate: the clean burn-in a collapse needs.
    assert by_flag["aged_clean"]["past_floor"] is True
    assert by_flag["aged_clean"]["divergences"] == 0
    assert by_flag["aged_clean"]["age_days"] == 31
    assert by_flag["aged_clean"]["recommend_collapse_eligible"] is True

    # Aged but the off-path fired: a divergence during burn-in blocks the recommend.
    assert by_flag["aged_noisy"]["past_floor"] is True
    assert by_flag["aged_noisy"]["divergences"] == 3
    assert by_flag["aged_noisy"]["recommend_collapse_eligible"] is False

    # Aged but no divergence gate: unmeasured, so burn-in is unconfirmable.
    assert by_flag["aged_unmeasured"]["past_floor"] is True
    assert by_flag["aged_unmeasured"]["divergences"] is None
    assert by_flag["aged_unmeasured"]["recommend_collapse_eligible"] is False


def test_burn_in_report_excludes_non_cutover_stages() -> None:
    """Only ``cutover``-stage entries produce rows.

    ``retain`` is a permanent fallback, and ``collapse_eligible`` / ``collapsed``
    are past the collapse decision — none of the three is a pending burn-in, so
    each is excluded from the rows entirely. Only the ``cutover`` entry remains.
    """
    today = date(2027, 2, 1)
    flags = (
        _cutover_entry("the_cutover", "cutover", "2027-01-01", gate="g"),
        _cutover_entry("retained", "retain", "2027-01-01", gate="g"),
        _cutover_entry("eligible", "collapse_eligible", "2027-01-01", gate="g"),
        _cutover_entry("done", "collapsed", "2027-01-01", gate="g"),
    )
    rows = cutover_burn_in_report(flags, {}, today)
    assert {row["flag"] for row in rows} == {"the_cutover"}


def test_burn_in_report_real_manifest_smoke() -> None:
    """The real manifest yields exactly its seven ``cutover``-stage entries.

    ``cutover_burn_in_report(CUTOVER_FLAGS, {}, FREEZE_DATE)`` returns a row for
    each ``cutover``-stage entry — the proposal-089
    ``observed_ready_standdown_enabled``, the committed-but-unpushed push
    actuator ``owned_committed_push_actuator_enabled``, the proposal-122
    account-dashboard navigation shell ``account_ui_v2``, and the proposal-128
    merge-queue cutover kill-switch ``tend_main_merge_enabled``, the proposal-130
    observer ``conscious_workspace_observe_enabled``, and the proposal-116
    levers ``generation_admission_enabled`` and
    ``retire_unlanded_limitations_enabled`` (the other three
    entries are ``retain``; proposal 111 collapsed ``live_work_corpus_enabled``
    and dropped its entry, and proposal 117 collapsed the proposal-099
    ``base_advance_re_entry_enabled`` / ``unwinnable_race_escalation_enabled`` pair
    to unconditional behaviour and dropped their entries). Each carries a ``None``
    ``gate`` (no divergence gate wired on today's tree), so
    ``recommend_collapse_eligible`` is ``False`` independent of the wall clock —
    burn-in cannot be confirmed without a signal.
    """
    rows = cutover_burn_in_report(CUTOVER_FLAGS, {}, FREEZE_DATE)
    by_flag = {row["flag"]: row for row in rows}
    cutover_flags = {
        "observed_ready_standdown_enabled",
        "owned_committed_push_actuator_enabled",
        "account_ui_v2",
        "tend_main_merge_enabled",
        "conscious_workspace_observe_enabled",
        "generation_admission_enabled",
        "retire_unlanded_limitations_enabled",
    }
    assert set(by_flag) == cutover_flags
    for flag in cutover_flags:
        assert by_flag[flag]["divergences"] is None
        assert by_flag[flag]["recommend_collapse_eligible"] is False


def test_tend_main_merge_flag_manifested_cutover_default_on() -> None:
    """Proposal 128's ``tend_main_merge_enabled`` is a default-on cut-over flag.

    The merge-queue cutover kill-switch ships default-on (flag-off skips the tend
    phase's ``origin/main`` merge-and-push while keeping every other tend role),
    so its flag-off branch is live logic and it belongs on the *cut-over* axis —
    a :class:`CutoverFlag`, never an :data:`EXPERIMENTAL_FLAGS` experiment. Mirrors
    ``tests/test_config_flag_lifecycle.py``'s ``account_ui_v2`` graduation guard:
    it is registered in ``CUTOVER_FLAGS`` with ``stage="cutover"`` and ``gate=None``
    (the tend merge emits no ``gate_activations.jsonl`` row), ``Config`` defaults it
    on, and it stays off the default-off manifest. The collapse-side structure
    (``off_path_symbol`` binding, growth budget) is held by the guards above.
    """
    from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS

    assert all(f.flag != "tend_main_merge_enabled" for f in EXPERIMENTAL_FLAGS), (
        "tend_main_merge_enabled is default-on and must not be in EXPERIMENTAL_FLAGS"
    )
    entry = next(
        (f for f in CUTOVER_FLAGS if f.flag == "tend_main_merge_enabled"),
        None,
    )
    assert entry is not None, (
        "tend_main_merge_enabled must be tracked in CUTOVER_FLAGS"
    )
    assert entry.stage == "cutover"
    assert entry.gate is None
    assert Config().tend_main_merge_enabled is True
