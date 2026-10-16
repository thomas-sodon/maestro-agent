"""Parameterized runner over the structural-guard registry (proposal 109).

Drives every registry entry in :data:`tests.structural_guards.CHOKEPOINT_GUARDS`
through the shared harness, reproducing each bespoke chokepoint guard's verdict:
the two in-memory parity controls (``synthetic_bypass`` flags, ``synthetic_ok``
does not) run first under the default 5s budget, then the three real-tree cases
(the live scan is clean, no allowlist entry is stale, every protected subject
still exists) run under the component budget. The whole-tree symbol reads are
hoisted into module-scoped fixtures so they happen once across all guards.

Each test is parametrized ``ids=[g.name for g in CHOKEPOINT_GUARDS]`` — the house
idiom from ``tests/test_fault_seeding.py`` — so a partial (or empty) registry
simply collects fewer cases without touching any bespoke guard file.
"""

from __future__ import annotations

import ast
import dataclasses
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from maestro.loop.lifecycle import _BUDGET_GATED_PHASES
from tests.structural_guards import (
    _BLOCKER_PIVOT,
    _BLOCKER_SOURCE_NAMES,
    _CI_TRUTH_GUARD,
    _GATE_OBSERVATION_GUARD,
    _GATE_TERMINAL_GUARD,
    _LIVE_WORK_GUARD,
    _MERGE_TRUTH_GUARD,
    _OWED_ACT_GUARD,
    _SESSION_ATTACHMENT_GUARD,
    _STANDDOWN_RELEASE_SITES_GUARD,
    _WIP_PRIMACY_GUARD,
    CHOKEPOINT_GUARDS,
    UNDECLARED_TOOLSET_BASELINE,
    _action_gate_installed_detector,
    _blocker_code_detector,
    _classification_signature,
    _gate_acceptance_detector,
    _retired_landing_bind_detector,
    undeclared_toolset_sites,
)
from tests.support.structural_guard import (
    _MAESTRO_ROOT,
    ChokepointGuard,
    CoOccurrenceRequirement,
    PartitionCoOccurrence,
    assert_allowlist_entries_live,
    assert_allowlist_entries_real,
    assert_co_occurrence,
    assert_flags_synthetic,
    assert_module_purity,
    assert_partition_co_occurrence,
    assert_required_presence,
    assert_scoped_reference,
    assert_subjects_exist,
    assert_synthetic_ok,
    bound_symbol_names,
    call_in_set_detector,
    functions_by_module,
    reference_in_set_detector,
    scan_for_violations,
    scan_source,
    symbols_defined_in,
)

_IDS = [guard.name for guard in CHOKEPOINT_GUARDS]

# Only guards carrying a ``required_presence`` requirement collect a presence case,
# so entries without one contribute no empty parametrization.
_PRESENCE_GUARDS = [guard for guard in CHOKEPOINT_GUARDS if guard.required_presence]
_PRESENCE_IDS = [guard.name for guard in _PRESENCE_GUARDS]

# Likewise, only guards carrying a ``scoped_references`` requirement collect a
# scoped case, so entries without one contribute no empty parametrization.
_SCOPED_GUARDS = [guard for guard in CHOKEPOINT_GUARDS if guard.scoped_references]
_SCOPED_IDS = [guard.name for guard in _SCOPED_GUARDS]

# Likewise, only guards carrying a ``co_occurrences`` requirement collect a
# co-occurrence case, so entries without one contribute no empty parametrization.
_CO_OCCURRENCE_GUARDS = [guard for guard in CHOKEPOINT_GUARDS if guard.co_occurrences]
_CO_OCCURRENCE_IDS = [guard.name for guard in _CO_OCCURRENCE_GUARDS]

# Likewise, only guards carrying a ``module_purity`` requirement collect a
# module-purity case, so entries without one contribute no empty parametrization.
_MODULE_PURITY_GUARDS = [guard for guard in CHOKEPOINT_GUARDS if guard.module_purity]
_MODULE_PURITY_IDS = [guard.name for guard in _MODULE_PURITY_GUARDS]

# Likewise, only guards carrying a ``partition_co_occurrences`` requirement collect a
# partition case, so entries without one contribute no empty parametrization.
_PARTITION_GUARDS = [
    guard for guard in CHOKEPOINT_GUARDS if guard.partition_co_occurrences
]
_PARTITION_IDS = [guard.name for guard in _PARTITION_GUARDS]

# Population budget: equal to today's registry count — the cap holds at parity
# with ``len(CHOKEPOINT_GUARDS)`` (the zero-sum ratchet
# ``test_cutover_flag_collapse.py`` pins), so a slot is spent only by a reviewed
# bump. Bumping this cap is allowed but is the exception — a bump not paired with
# a compensating retirement (or a subject migrated away) is exactly the
# addition-beats-deletion signal this budget exists to catch. Keep it a literal
# integer, not ``len(...) + N``, so it fails the build when the population grows
# rather than silently tracking it.
# Bumped 16→17 for proposal 112's net-new frame_discharge sole-owner guard (no
# existing owner it subsumes, so no compensating retirement is available).
# Bumped 17→18 for proposal 118's net-new next_eligible_slice sole-owner guard
# (a brand-new oracle with no prior owner it subsumes, so no compensating
# retirement is available).
# Bumped 18→19 for proposal 120's net-new generation_authority sole-owner guard
# (a brand-new generation-side routing anchor with no prior owner it subsumes, so
# no compensating retirement is available).
# Bumped 19→20 for proposal 114's net-new plan_grounding sole-owner guard (a
# brand-new plan-premise grounding vocabulary owner with no prior owner it
# subsumes, so no compensating retirement is available).
# Bumped 20→21 for issue #2914's net-new action_gate_installation guard. It
# enforces a declaration-to-installation contract — a Bash-granting
# ``QueryOptions`` must carry hooks built by a recognized action-gate builder,
# not merely a ``hooks=`` keyword — that no existing entry covers: the
# capability gate's own validator asserts only provider-name trust, and the
# session-attachment guard fences which *call* is used, not how the options are
# built. No compensating retirement is available.
# Bumped 21→22 for proposal 129's net-new calibration_verdict_authority guard. It
# pins a brand-new owner — author_reflection_verdict / AuthoredVerdict decide what
# verdict the reflection ledger records — and clamps record_reflection_verdict to
# the one scope that authors through that oracle; no prior owner it subsumes, so no
# compensating retirement is available.
# Bumped 22→24 for proposal 124's two net-new gate-observation entries. The
# gate_observation guard pins gate_observation.py as the sole owner of the local
# gate-failure vocabulary; the gate_terminal guard pins
# EvolutionEngine._convergence_terminal as the sole ConvergenceResult constructor.
# Both are net-new invariants with no prior owner they subsume, so no compensating
# retirement is available, and neither folds into the other — one excludes its
# owner module and carries an empty allowlist, the other scans the whole tree and
# confines by allowlist.
# Bumped 24→25 for proposal 131's net-new attempt_disposition guard. It pins a
# brand-new owner — attempt_disposition.py as the sole definer of classify_attempt /
# AttemptDisposition / AttemptVerdict — and, through a co_occurrences requirement,
# binds every implement_plan demote/hold terminal to an oracle consult. No prior
# owner it subsumes, so no compensating retirement is available.
# Bumped 26→27 for proposal 135's net-new standdown_validity sole-owner guard. It
# pins standdown_validity.py as the sole owner of what a latched stand-down claims
# (StanddownClaim / StanddownValidity / assess_standdown_validity) and, via two
# module-purity entries, as a pure leaf that neither reads the escalation ledger nor
# touches the filesystem or network. A brand-new claim-validity vocabulary with no
# prior owner it subsumes — nothing is retired by its arrival — so no compensating
# retirement is available.
#
# Bumped 25→26 for proposal 132's net-new reflection_retrieval sole-owner guard. It
# pins reflection_retrieval.py as the sole owner of the bounded read-only reflection
# retrieval surface's vocabulary (READ_ONLY_REFLECTION_TOOLS /
# MUTATING_INTROSPECTION_TOOLS / build_reflection_tool_set) and, via a module-purity
# entry, as a leaf that reaches no SDK seam. A brand-new read-only tool-surface
# vocabulary with no prior owner it subsumes, so no compensating retirement is
# available.
# Bumped 27→28 for proposal 135's net-new standdown_release_sites registry. It
# enumerates the functions in escalation_bridge.py that may delete a standdown
# record, so a fourth release path cannot appear unregistered beside
# clear_standdown / prune_resolved_standdowns / prune_expired_standdowns /
# prune_refuted_standdowns — a latch released by an unreviewed path is a
# suppression silently lifted. Orthogonal to its sibling standdown_validity guard
# (that one owns what a claim *means*, this one who may *act* on it), and no prior
# guard enumerated the release sites, so no compensating retirement is available.
# Bumped 28→29 for proposal 137's net-new owed_act registry entry. It pins
# pr_owed_act.classify_pr_owed_act as the sole owner of the owed-act partition —
# no scope may fold the ownership axis (classify_blocker_ownership /
# classify_owned_blocker) and the CI axis (classify_ci_conclusions) into its own
# private verdict, and no module outside the owner may mint an OwedAct member.
# A brand-new owed-act ownership vocabulary: the merge-truth and CI-truth entries
# each own a different question (has it landed? is this check red?), neither the
# act the PR owes right now, so this entry subsumes no prior guard and no
# compensating retirement is available.
# Bumped 29→30 for issue #4020's net-new message_ingestion entry. It pins
# MaestroAgent._process_messages as the sole writer of Message.read so a display
# tool cannot consume a message before the loop enqueues it. No existing entry
# owns the messaging store, so no compensating retirement is available.
# Bumped 30→31 for proposal 143's net-new tree_retrieval entry. It pins
# tree_retrieval.py as the sole owner of confined, read-only tree reads; no
# existing entry owns filesystem-read confinement, so no compensating retirement
# is available.
# Bumped 31→32 for proposal 136's net-new reality_inventory entry. It pins
# reality_inventory.py as the sole owner of the shipped-capability /
# measured-problem derivation and the mint clearance, and binds
# submit_for_review's notify to a clear_mint call in the same scope. It nests
# inside tree_retrieval (the inventory derives the sets, tree_retrieval performs
# the reads) and duplicates none of it, so no compensating retirement is
# available.
# Bumped 32→33 for proposal 144's net-new net_burn_owner entry. It pins
# net_burn.py as the sole constructor of the value types and prompts/defaults.py
# as the sole home of the charter prose (its objective-read arm was retired by
# proposal 155 Step 3); no existing entry owns plan worth, so no compensating
# retirement is available.
# Bumped 33→34 for proposal 152's net-new charter_owner entry: no prior owner
# subsumes solvency arithmetic, so no compensating retirement is available.
# Bumped 34→37 for proposal 150's net-new decision_door_owner, charter_store_owner
# and taint_owner entries: no prior owner covers the human-decision write path, the
# charter SQLite store or external-text fencing, so no compensating retirement is
# available.
# Bumped 37→38 for proposal 153's net-new human_task_owner entry: no prior
# owner holds the human task ledger, so no compensating retirement is available.
# Bumped 38→39 for proposal 155's net-new gap_issue_owner entry: gap issues
# are a new trust boundary with no prior owner to retire in its place.
# Lowered 39→35 when the revenue lineage was deleted: net_burn_owner,
# charter_owner, decision_door_owner and human_task_owner retired with their
# subjects, and charter_store_owner was retargeted as gap_store_owner.
_MAX_STRUCTURAL_GUARDS = 35


def _population_within_budget(count: int) -> bool:
    """True while the registry holds no more than :data:`_MAX_STRUCTURAL_GUARDS`."""
    return count <= _MAX_STRUCTURAL_GUARDS


@pytest.fixture(scope="module")
def maestro_symbols() -> set[str]:
    """Every symbol defined under ``maestro/``, read once for the retirement trigger.

    Injected into :func:`assert_subjects_exist` so the whole-tree read is shared
    across every guard rather than repeated per entry.
    """
    return symbols_defined_in(_MAESTRO_ROOT)


@pytest.fixture(scope="module")
def maestro_functions_by_basename() -> dict[str, set[str]]:
    """Basename -> defined function names across the whole tree, read once.

    A superset covering every guard's ``scanned`` set — the two-file (merge-truth)
    and whole-tree-except-owner (CI-truth) allowlist checks both resolve from this
    single read, injected into :func:`assert_allowlist_entries_real`.
    """
    return functions_by_module(sorted(_MAESTRO_ROOT.rglob("*.py")))


@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_synthetic_bypass_is_flagged(guard: ChokepointGuard) -> None:
    """Negative control: each entry's ``synthetic_bypass`` flags at least one node.

    In-memory (default 5s budget). A bypass that flags nothing means the detector
    no longer catches its own known violation — the guard has gone silently dead.
    """
    assert_flags_synthetic(guard)


@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_synthetic_ok_is_clean(guard: ChokepointGuard) -> None:
    """Positive control: each entry's ``synthetic_ok`` flags nothing.

    In-memory (default 5s budget). An OK snippet that flags means the detector
    over-fires on a legitimate pattern.
    """
    assert_synthetic_ok(guard)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_live_scan_is_clean(guard: ChokepointGuard) -> None:
    """The live scan over the real ``maestro/`` tree is clean per entry.

    Reproduces the bespoke guard's verdict: no merge-deciding primitive is called
    outside the ``is_pr_settled`` chokepoint, and no CI-classification primitive
    appears outside the ``ci_diagnosis`` owner. Reads real source — component budget.
    """
    assert scan_for_violations(guard) == []


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_allowlist_entries_are_real(
    guard: ChokepointGuard, maestro_functions_by_basename: dict[str, set[str]]
) -> None:
    """No allowlist entry names a function that has vanished (staleness check)."""
    assert_allowlist_entries_real(guard, by_module=maestro_functions_by_basename)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_allowlist_entries_exempt_a_live_hit(guard: ChokepointGuard) -> None:
    """Every allowlist entry still shields a live hit, so the exempt surface only shrinks."""
    assert_allowlist_entries_live(guard)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_subjects_still_exist(guard: ChokepointGuard, maestro_symbols: set[str]) -> None:
    """Every protected subject still exists — the retirement trigger."""
    assert_subjects_exist(guard, defined=maestro_symbols)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", _PRESENCE_GUARDS, ids=_PRESENCE_IDS)
def test_required_presence_holds(guard: ChokepointGuard) -> None:
    """Every ``required_presence`` requirement holds over the real ``maestro/`` tree.

    The presence counterpart to :func:`test_live_scan_is_clean`: where that scan
    fails on a *violating* node, this fails when a *required* import is absent.
    Reproduces the retired ``test_provider_session_handle.py``'s
    ``test_session_typed_via_providers_base`` — each of the nine loop consumers
    imports the opaque ``maestro.providers.base.Session`` handle. Reads real source
    — component budget.
    """
    for requirement in guard.required_presence:
        assert_required_presence(requirement)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", _SCOPED_GUARDS, ids=_SCOPED_IDS)
def test_scoped_references_hold(guard: ChokepointGuard) -> None:
    """Every ``scoped_references`` requirement holds over the real ``maestro/`` tree.

    The function-scoped counterpart to :func:`test_required_presence_holds`: where
    that asserts a required *import* at module scope, this asserts a detector matches
    (or does not match) *inside one named function*. Reproduces the three in-function
    checks the retired origin-grounded bespoke guard held — the grounded observer is
    consulted inside ``tend_evolution_prs``, the host-proxy helper is absent from it,
    and the ``reset_hard("origin/main")`` heal call is present. Reads real source —
    component budget.
    """
    for requirement in guard.scoped_references:
        assert_scoped_reference(requirement)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", _CO_OCCURRENCE_GUARDS, ids=_CO_OCCURRENCE_IDS)
def test_co_occurrences_hold(guard: ChokepointGuard) -> None:
    """Every ``co_occurrences`` requirement holds over the real ``maestro/`` tree.

    The scope-level counterpart to :func:`test_scoped_references_hold`: where that
    asserts a detector inside *one named* function, this asserts that, across a module,
    every function where a *trigger* fires also matches a *companion*. Reproduces the
    two relational checks the retired ``test_pr_salvage_chokepoint.py`` held — every
    ``agent.py`` disengage site that references a ``Salvage`` verdict consults the
    oracle gatherer, and every give-up author consults it before persisting a
    stand-down. Reads real source — component budget.
    """
    for requirement in guard.co_occurrences:
        assert_co_occurrence(requirement)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", _MODULE_PURITY_GUARDS, ids=_MODULE_PURITY_IDS)
def test_module_purity_holds(guard: ChokepointGuard) -> None:
    """Every ``module_purity`` requirement holds over the real ``maestro/`` tree.

    The module-scoped counterpart to :func:`test_scoped_references_hold`: where that
    asserts a detector inside *one named* function, this asserts a whole module neither
    imports a forbidden dotted prefix nor references any forbidden name. Reproduces the
    retired ``test_owned_branch_oracle.py``'s ``test_gate_module_never_re_derives_
    ownership`` — ``maestro/tool_gating.py`` consumes the injected ``allowed_branches``
    set and never re-derives ownership from the folded-in oracle. Reads real source —
    component budget.
    """
    for requirement in guard.module_purity:
        assert_module_purity(requirement)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("guard", _PARTITION_GUARDS, ids=_PARTITION_IDS)
def test_partition_co_occurrences_hold(guard: ChokepointGuard) -> None:
    """Every ``partition_co_occurrences`` requirement holds over the real ``maestro/`` tree.

    The tree-wide scope-aggregate counterpart to :func:`test_co_occurrences_hold`: where
    that asserts a relationship across one module's functions, this asserts that no scope
    in *any* scanned module names the pivot reason code plus another distinct code without
    consulting the classifier in that same scope. Reproduces the retired
    ``test_blocker_ownership_binding.py``'s ``test_no_consumer_re_derives_blocker_
    ownership``. Reads real source — component budget.
    """
    for requirement in guard.partition_co_occurrences:
        assert_partition_co_occurrence(requirement)


@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS, ids=_IDS)
def test_provenance_is_present(guard: ChokepointGuard) -> None:
    """Every entry carries non-empty ``provenance`` naming its invariant or owner.

    In-memory (default 5s budget). Provenance is the machine-readable record of
    *why* the guard exists (which proposal or incident minted the invariant it
    protects); an entry registered without it guards a rule no one can trace, so
    no future entry may be added blank.
    """
    assert guard.provenance.strip(), (
        f"guard {guard.name}: provenance is empty — name the proposal or "
        "invariant this entry protects"
    )


def test_registry_population_within_budget() -> None:
    """The live registry fits its population budget (proposal 109 step 7).

    In-memory (default 5s budget). A single hard cap on ``len(CHOKEPOINT_GUARDS)``
    makes adding a guard a zero-sum decision — retire an existing entry (or migrate
    its subject away), or raise ``_MAX_STRUCTURAL_GUARDS`` in the same reviewed diff
    and say why. A cap bump not paired with a compensating retirement is the
    addition-beats-deletion signal this budget exists to catch.
    """
    count = len(CHOKEPOINT_GUARDS)
    assert _population_within_budget(count), (
        f"registry now holds {count} guards against the cap of "
        f"{_MAX_STRUCTURAL_GUARDS}: retire an entry (or migrate its subject away), "
        "or raise _MAX_STRUCTURAL_GUARDS in the same reviewed diff and justify it"
    )


def test_population_budget_bites_past_the_cap() -> None:
    """The budget predicate is a real numeric bound, not a vacuous ``True``.

    In-memory (default 5s budget). Accepts the boundary count and rejects one past
    the cap — without mutating the real registry — so a future refactor that turned
    ``_population_within_budget`` into an always-true stub is caught here. Together
    with :func:`test_registry_population_within_budget` (the True path) this
    exercises both branches of the predicate.
    """
    assert _population_within_budget(_MAX_STRUCTURAL_GUARDS) is True
    assert _population_within_budget(_MAX_STRUCTURAL_GUARDS + 1) is False


# ── Meta-guard: no un-registered skeleton files (proposal 109 step 7) ───────────
#
# The population budget above makes *adding a registry entry* a zero-sum decision.
# This meta-guard closes the other bypass: dropping a fresh standalone
# ``test_*_chokepoint.py`` that copy-pastes the bespoke skeleton — the tell-tale
# module-level ``_line_to_function`` line->function mapper every retired guard
# carried — routing around the registry and its budget entirely. The predicate
# below flags any test file that re-defines that skeleton outside the sanctioned
# harness home and the documented allowlist.

_TESTS_ROOT = Path(__file__).resolve().parent

# The harness home legitimately owns the canonical mapper — but under the
# no-underscore name ``line_to_function`` (see tests/support/structural_guard.py),
# so it is never a ``_line_to_function`` offender. Named here so re-introducing the
# underscore form in the harness itself would be a conscious edit, not a silent pass.
_SANCTIONED_SKELETON_HOME = "structural_guard.py"

# Test files that carry a module-level ``_line_to_function`` by design, each exempt
# for a stated reason. The allowlist is self-pruning: ``test_skeleton_allowlist_
# names_live_carriers`` fails if any entry stops defining the skeleton (migrated or
# cleaned up), naming the stale entry to remove.
_ALLOWED_SKELETON_CARRIERS = frozenset(
    {
        # Transcript-reader chokepoint, co-located with its behavioral suite rather
        # than migrated into CHOKEPOINT_GUARDS.
        "test_conversation_transcript.py",
        # The whole-tree ``except``-scan guard keeps its own mapper copy by design;
        # it is a differently-shaped guard, outside the registry's scope.
        "test_no_silenced_exceptions.py",
        # Client-construction chokepoint, a later-minted guard awaiting migration
        # into the registry (see proposal 109 "Out of scope").
        "test_work_target_chokepoint.py",
    }
)


def _defines_module_level_skeleton(source: str) -> bool:
    """True if *source*'s module body defines a ``_line_to_function`` function.

    Keys on a **module-level** ``FunctionDef``/``AsyncFunctionDef`` in ``tree.body``
    — a ``_line_to_function`` nested inside a class or function is not the copy-paste
    skeleton and does not count.
    """
    tree = ast.parse(source)
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_line_to_function"
        for node in tree.body
    )


def _files_defining_bespoke_skeleton(sources: dict[str, str]) -> list[str]:
    """Paths in *sources* that re-define the bespoke guard skeleton.

    A source-text-only predicate (no filesystem access, mirroring
    :func:`_population_within_budget`) so the synthetic control can drive every
    branch. *sources* is keyed by each file's path **relative to the tests root**
    (a bare basename is a valid degenerate relative path), so two files sharing a
    basename in different directories are distinct keys and neither masks the other
    — a clean same-basename file elsewhere cannot hide a real offender. A pair is an
    offender when it defines a module-level ``_line_to_function`` and its *basename*
    (the exemptions are basename-scoped) is neither the sanctioned harness home nor a
    documented allowlist carrier. Returned sorted for a stable failure message.
    """
    return sorted(
        relpath
        for relpath, source in sources.items()
        if Path(relpath).name != _SANCTIONED_SKELETON_HOME
        and Path(relpath).name not in _ALLOWED_SKELETON_CARRIERS
        and _defines_module_level_skeleton(source)
    )


def test_skeleton_meta_guard_bites() -> None:
    """The predicate flags offenders and exonerates the exempt shapes.

    In-memory (default 5s budget), driving every branch without touching the real
    tree. Keys are paths relative to the tests root (a bare basename is a degenerate
    relative path), so this also pins the no-basename-masking property: a module-level
    ``def _line_to_function`` is flagged whether it sits at the top level or under a
    subdirectory — a clean same-basename file elsewhere cannot hide a real offender;
    the same skeleton under an allowlisted basename and under the sanctioned home is
    exempt *even beneath a subdirectory path* (the exemptions are basename-scoped, and
    the real harness lives at ``support/structural_guard.py``); a ``_line_to_function``
    nested inside a class (not module-level) is not the skeleton; and a clean source
    contributes nothing.
    """
    skeleton = "def _line_to_function(tree):\n    return {}\n"
    nested = (
        "class Guard:\n"
        "    def _line_to_function(self, tree):\n"
        "        return {}\n"
    )
    clean = "def helper():\n    return 1\n"
    allowlisted = next(iter(_ALLOWED_SKELETON_CARRIERS))
    sources = {
        "test_zz_new_chokepoint.py": skeleton,
        "component/test_zz_sub_chokepoint.py": skeleton,
        # Same basename, one clean at top level and one skeleton in a subdirectory:
        # under basename keying the clean one could mask the offender; under path
        # keying both survive and the subdir offender is still caught.
        "test_masked.py": clean,
        "component/test_masked.py": skeleton,
        allowlisted: skeleton,
        f"support/{allowlisted}": skeleton,
        _SANCTIONED_SKELETON_HOME: skeleton,
        f"support/{_SANCTIONED_SKELETON_HOME}": skeleton,
        "test_nested_skeleton.py": nested,
        "test_clean_guard.py": clean,
    }
    assert _files_defining_bespoke_skeleton(sources) == [
        "component/test_masked.py",
        "component/test_zz_sub_chokepoint.py",
        "test_zz_new_chokepoint.py",
    ]


@pytest.mark.timeout(15)
def test_no_unregistered_skeleton_files() -> None:
    """No un-registered test file re-defines the bespoke guard skeleton.

    Whole-tree scan (component budget): reads every ``*.py`` under ``_TESTS_ROOT``,
    keyed by path **relative to** ``_TESTS_ROOT`` so two files sharing a basename in
    different directories stay distinct (a clean same-basename file cannot mask an
    offender), and asserts none outside the sanctioned home and the allowlist carries
    the copy-paste ``_line_to_function`` skeleton. A fresh standalone chokepoint guard
    file is exactly the accretion the registry + budget exist to end; adding one turns
    this red until the guard is migrated into the registry or allowlisted with a reason.
    """
    sources = {
        path.relative_to(_TESTS_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_TESTS_ROOT.rglob("*.py"))
    }
    assert sources, f"no test sources read under {_TESTS_ROOT} — broken scan root"
    offenders = _files_defining_bespoke_skeleton(sources)
    assert offenders == [], (
        "un-registered structural-guard skeleton file(s): "
        + ", ".join(offenders)
        + " — each defines a module-level `_line_to_function`, the copy-paste guard "
        "skeleton. Migrate the guard into CHOKEPOINT_GUARDS "
        "(tests/structural_guards.py), or — if it is a genuinely differently-shaped "
        "guard — add it to _ALLOWED_SKELETON_CARRIERS with a documented reason."
    )


@pytest.mark.timeout(15)
def test_skeleton_allowlist_names_live_carriers() -> None:
    """Every allowlist entry still names a live skeleton carrier (self-pruning).

    Whole-tree scan (component budget): each basename in
    :data:`_ALLOWED_SKELETON_CARRIERS` must exist under ``_TESTS_ROOT`` and still
    define a module-level ``_line_to_function`` today. Once a carrier is migrated
    into the registry (or drops its skeleton), its stale allowlist entry turns the
    build red and names the entry to remove — the orphan-surfacing discipline the
    subject-existence trigger already applies to guard subjects.
    """
    by_name = {path.name: path for path in _TESTS_ROOT.rglob("*.py")}
    for carrier in sorted(_ALLOWED_SKELETON_CARRIERS):
        path = by_name.get(carrier)
        assert path is not None, (
            f"allowlisted skeleton carrier {carrier} no longer exists under "
            f"{_TESTS_ROOT} — remove the stale _ALLOWED_SKELETON_CARRIERS entry"
        )
        assert _defines_module_level_skeleton(path.read_text(encoding="utf-8")), (
            f"allowlisted carrier {carrier} no longer defines a module-level "
            "`_line_to_function` — it was migrated or cleaned up; remove its stale "
            "_ALLOWED_SKELETON_CARRIERS entry"
        )


# ── In-runner tail-guard ledger ────────────────────────────────────────────────
#
# The population budget governs ``CHOKEPOINT_GUARDS`` entries and the meta-guard
# above closes the standalone-``test_*_chokepoint.py`` bypass. A third route stays
# open by design: a bespoke AST guard written as a plain ``def test_*`` in *this*
# file, parsing a live ``maestro/`` module directly. Those guards assert a branch
# *shape* inside a named function (a cadence modulo, a flag-gated consumer) rather
# than classifying a single node over the whole tree, so the registry's detector
# contract cannot express them without distorting it — the route is legitimate.
# What it lacked was a ledger: the guards below were invisible to both fences,
# so effective guard mass could grow past the stated cap without a reviewed diff.
# :data:`_TAIL_GUARD_NAMES` enumerates them, :data:`_MAX_TAIL_GUARDS` bounds them
# under the same zero-sum rule the registry budget uses, and
# :func:`_bespoke_tail_guards` makes an unledgered one fail loud until it is ledgered.

_TAIL_GUARD_NAMES: frozenset[str] = frozenset(
    {
        # Proposal 113 — a priced budget-gated phase must carry a cadence branch.
        "test_priced_introspection_phase_is_cadence_gated",
        # Proposal 116 step 9 — the three task↔evolution decoupling master switches.
        "test_source_task_id_consumption_is_reuse_flag_gated",
        "test_generate_task_scheduling_is_master_switch_gated",
        "test_promotion_pressure_sites_name_master_switch",
        # Issue #4127 — a shrink-only baseline of tool-undeclared QueryOptions; a
        # set-equality ratchet, not a detector/allowlist shape the registry models.
        "test_undeclared_toolset_ratchet_is_shrink_only",
    }
)

# A literal integer, not ``len(_TAIL_GUARD_NAMES)`` — a self-sizing cap is not a
# cap. Raising it is the reviewed diff the registry budget already demands.
_MAX_TAIL_GUARDS = 5


def _tail_guards_within_budget(count: int) -> bool:
    """True while the runner holds no more than :data:`_MAX_TAIL_GUARDS` tail guards."""
    return count <= _MAX_TAIL_GUARDS


def _referenced_names(nodes: Iterable[ast.AST]) -> set[str]:
    """Every :class:`ast.Name` id appearing anywhere under *nodes*."""
    return {
        child.id
        for node in nodes
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }


def _live_source_constants(tree: ast.Module) -> set[str]:
    """``_MAESTRO_ROOT`` plus the module-level constants derived from it.

    Discovered from the parsed tree rather than hardcoded, so a fresh
    ``_FOO_PATH = _MAESTRO_ROOT / "foo.py"`` is recognised as a live-source read
    without editing this predicate. A single forward pass in document order, so a
    constant derived from another derived constant counts as long as it is assigned
    below its base (the house layout); only plain ``Name`` targets bind.
    """
    live = {"_MAESTRO_ROOT"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and _referenced_names([node.value]) & live:
            live |= {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
    return live


def _bespoke_tail_guards(source: str) -> list[str]:
    """Names of *source*'s module-level in-runner tail guards, sorted.

    A source-text-only predicate (no filesystem access, mirroring
    :func:`_files_defining_bespoke_skeleton`) so the synthetic control can drive
    every branch. A module-level ``def test_*`` is a tail guard when it (a) reads a
    live ``maestro/`` source — it references ``_MAESTRO_ROOT`` or a constant derived
    from it per :func:`_live_source_constants` — and (b) references no registry
    object: neither ``CHOKEPOINT_GUARDS`` nor any ``_..._GUARD`` name. Both facts are
    computed over the test's decorators and body *expanded one level through
    module-level helper calls*, so a guard reaching its registry entry through a
    helper (``_scan_agent_for_session_attachment``) or through a
    ``@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS)`` decorator is correctly
    exempt, while a tail guard whose helpers touch no registry object is not. A
    module-scoped fixture is expanded by the same one-level rule and so confers no
    exemption of its own: injecting ``maestro_symbols`` pulls ``_MAESTRO_ROOT`` in,
    making the test a live-source reader that still needs a registry name to be
    exempt — which is why :func:`test_subjects_still_exist` rides its
    ``parametrize`` decorator, not its fixture.
    Returned sorted for a stable failure message.
    """
    tree = ast.parse(source)
    live = _live_source_constants(tree)
    module_defs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    helpers = {
        node.name: node for node in module_defs if not node.name.startswith("test_")
    }
    offenders = []
    for test in module_defs:
        if not test.name.startswith("test_"):
            continue
        names = _referenced_names(test.decorator_list) | _referenced_names(test.body)
        for helper in names & set(helpers):
            names |= _referenced_names(helpers[helper].body)
        rides_registry = "CHOKEPOINT_GUARDS" in names or any(
            name.endswith("_GUARD") for name in names
        )
        if names & live and not rides_registry:
            offenders.append(test.name)
    return sorted(offenders)


def test_tail_guard_detector_flags_and_exempts() -> None:
    """The predicate flags an unledgered tail guard and exonerates the exempt shapes.

    In-memory (default 5s budget), driving every branch over a synthetic runner
    source without touching the real tree. Flagged: a tail-guard-shaped test reading
    a ``_MAESTRO_ROOT``-derived path constant and naming no registry object — a fifth
    guard landing with every existing fence green is exactly the bad state this rule
    exists to catch — as is a test whose only live-source reach is an injected
    module-scoped fixture, pinning that a fixture confers no exemption of its own.
    Exempt: the same shape reaching a ``_GUARD`` object through a
    module-level helper; a test naming a ``_GUARD`` directly; a test riding the
    registry through its ``parametrize`` decorator; and a pure in-memory test that
    reads no live source at all. Also pins the discovery rules — a constant derived
    from a *derived* constant still counts, an assignment not derived from
    ``_MAESTRO_ROOT`` does not, a non-``Name`` assignment target binds nothing, and
    an ``async def test_*`` is scanned like a sync one.
    """
    source = (
        '_MAESTRO_ROOT = Path("maestro")\n'
        '_LIFECYCLE_PATH = _MAESTRO_ROOT / "loop" / "lifecycle.py"\n'
        '_NESTED_PATH = _LIFECYCLE_PATH.parent / "other.py"\n'
        '_UNRELATED_PATH = Path("elsewhere.py")\n'
        "_PAIR, _OTHER = _MAESTRO_ROOT, 1\n"
        "def _reach_registry():\n"
        "    return _MERGE_TRUTH_GUARD\n"
        "def _plain_helper():\n"
        "    return _LIFECYCLE_PATH.read_text()\n"
        "def test_tail_guard_shaped():\n"
        "    assert _LIFECYCLE_PATH.read_text()\n"
        "async def test_async_tail_guard_shaped():\n"
        "    assert _NESTED_PATH.read_text()\n"
        "def test_via_helper_reaching_registry():\n"
        "    assert _plain_helper() and _reach_registry()\n"
        "def test_names_guard_directly():\n"
        "    assert _CI_TRUTH_GUARD and _LIFECYCLE_PATH.read_text()\n"
        '@pytest.mark.parametrize("guard", CHOKEPOINT_GUARDS)\n'
        "def test_rides_registry_via_decorator(guard):\n"
        "    assert _plain_helper()\n"
        "def test_pure_in_memory():\n"
        "    assert _UNRELATED_PATH and _PAIR_IS_NOT_BOUND is None\n"
        "def maestro_symbols():\n"
        '    return _MAESTRO_ROOT.rglob("*.py")\n'
        "def test_injects_fixture_only(maestro_symbols):\n"
        "    assert maestro_symbols\n"
    )
    assert _bespoke_tail_guards(source) == [
        "test_async_tail_guard_shaped",
        "test_injects_fixture_only",
        "test_tail_guard_shaped",
    ]


def test_tail_guard_population_within_budget() -> None:
    """The live tail-guard ledger fits its own population budget.

    In-memory (default 5s budget). The same zero-sum rule
    :func:`test_registry_population_within_budget` applies to the registry: a fifth
    in-runner guard means retiring one, migrating one into ``CHOKEPOINT_GUARDS``, or
    raising the cap in the same reviewed diff with a reason.
    """
    count = len(_TAIL_GUARD_NAMES)
    assert _tail_guards_within_budget(count), (
        f"the runner now ledgers {count} in-file tail guards against the cap of "
        f"{_MAX_TAIL_GUARDS}: retire one (or migrate it into CHOKEPOINT_GUARDS), "
        "or raise _MAX_TAIL_GUARDS in the same reviewed diff and justify it"
    )


def test_tail_guard_budget_bites_past_the_cap() -> None:
    """The tail-guard budget predicate is a real numeric bound, not a vacuous ``True``.

    In-memory (default 5s budget). Accepts the boundary count and rejects one past
    the cap, so a future refactor that turned ``_tail_guards_within_budget`` into an
    always-true stub is caught here. Together with
    :func:`test_tail_guard_population_within_budget` (the True path) this exercises
    both branches of the predicate.
    """
    assert _tail_guards_within_budget(_MAX_TAIL_GUARDS) is True
    assert _tail_guards_within_budget(_MAX_TAIL_GUARDS + 1) is False


@pytest.mark.timeout(15)
def test_in_file_tail_guards_are_enumerated() -> None:
    """Every in-runner tail guard is named in the ledger, and vice versa.

    One real file read (component budget): runs :func:`_bespoke_tail_guards` over
    this module's own source and asserts set equality with
    :data:`_TAIL_GUARD_NAMES`. Equality is bidirectional, so the ledger is
    self-pruning in both directions — a fresh unenumerated tail guard turns this red,
    and so does a renamed or deleted one whose stale entry lingers, the way
    :func:`test_skeleton_allowlist_names_live_carriers` keeps its allowlist honest.
    """
    found = set(_bespoke_tail_guards(Path(__file__).read_text(encoding="utf-8")))
    unledgered = sorted(found - _TAIL_GUARD_NAMES)
    assert not unledgered, (
        "un-ledgered in-file structural tail guard(s): "
        + ", ".join(unledgered)
        + " — each parses a live maestro/ source from inside this runner while "
        "naming no registry object, so neither _MAX_STRUCTURAL_GUARDS nor the "
        "skeleton meta-guard sees it. Migrate the guard into CHOKEPOINT_GUARDS "
        "(tests/structural_guards.py), retiring an entry or raising "
        "_MAX_STRUCTURAL_GUARDS — or add it to _TAIL_GUARD_NAMES and raise "
        "_MAX_TAIL_GUARDS in the same reviewed diff with a reason."
    )
    stale = sorted(_TAIL_GUARD_NAMES - found)
    assert not stale, (
        "stale _TAIL_GUARD_NAMES entr(y/ies): "
        + ", ".join(stale)
        + " — no longer a live in-file tail guard here (renamed, deleted, or "
        "migrated into the registry); remove the entry and lower _MAX_TAIL_GUARDS."
    )


# ── Detector-branch hardening ──────────────────────────────────────────────────
#
# The live-work, CI-truth, merge-truth, and WIP-primacy entries' ``synthetic_bypass``
# / ``synthetic_ok`` controls exercise only a subset of each detector's shapes — the
# plain ``==`` shape for live-work / CI-truth, only the ``ast.Attribute`` call shape
# for merge-truth, and only the ``Finish PR #`` marker for WIP-primacy — so the wider
# detector branches — the bare-``status`` name operand, the ``in`` / ``not in``
# membership shape, the non-live near-miss literals, the ambiguous conclusion tokens
# that must *not* flag, the bare ``ast.Name`` primitive call, and the ``Hand off PR #``
# handoff marker — were, until their bespoke twins were retired, covered only by the
# retired files' dedicated detector tests. These cases port that coverage into the
# registry so those branches keep an explicit assertion, not just the real-tree scan.


def _scan_with(guard: ChokepointGuard, source: str) -> list[str]:
    """Run *guard*'s detector over *source* with its resolver, no allowlist."""
    return scan_source(
        source,
        guard.detector,
        frozenset(),
        "<t>",
        line_resolver=guard.line_resolver,
        exempt_lines=guard.exempt_lines,
    )


def _flagged_lines(hits: list[str]) -> list[int]:
    """Sorted line numbers parsed from the ``"<t>:<line> — …"`` violation strings."""
    return sorted(int(hit.split(":", 1)[1].split(" ", 1)[0]) for hit in hits)


def test_live_work_detector_flags_bare_name_and_membership_shapes() -> None:
    """Bare-``status`` name operand and ``in`` / ``not in`` container shapes flag.

    Reproduces the detector coverage the retired ``test_live_work_chokepoint.py``
    carried that the registry entry's ``==``-only ``synthetic_bypass`` does not
    exercise: a bare ``status`` local (not a ``.status`` attribute) and a membership
    test against a tuple literal, in both operand orders.
    """
    source = (
        "def corpora(self, plan):\n"
        "    status = plan.status\n"
        '    a = status == "approved"\n'
        '    b = plan.status not in ("pending_review", "approved")\n'
        '    c = "implemented" == plan.status\n'
        "    return a or b or c\n"
    )
    hits = _scan_with(_LIVE_WORK_GUARD, source)
    assert _flagged_lines(hits) == [3, 4, 5]
    joined = "\n".join(hits)
    assert "['approved']" in joined
    assert "['approved', 'pending_review']" in joined
    assert "['implemented']" in joined


def test_live_work_detector_ignores_non_live_and_out_of_scope_literals() -> None:
    """Comparisons against non-live / out-of-scope status literals never flag.

    ``PlanStep`` (``"done"``), the exact-title corpus (``"failed"``), the rejection
    cooldown (``"rejected"``), and ``PublicationProposal`` (``"pending_approval"`` —
    a near-miss on the live ``"pending_review"``) are structurally out of scope, so
    they need no allowlist entry. Reproduces the retired dedicated detector test.
    """
    source = (
        "def unrelated(self, step, plan, pub):\n"
        '    a = step.status == "done"\n'
        '    b = plan.status == "failed"\n'
        '    c = plan.status != "rejected"\n'
        '    d = pub.status == "pending_approval"\n'
        '    e = plan.status in ("rejected", "obsolete")\n'
        "    return a or b or c or d or e\n"
    )
    assert _scan_with(_LIVE_WORK_GUARD, source) == []


def test_ci_truth_detector_flags_vocab_and_name_fallback_not_ambiguous() -> None:
    """Distinctive vocab tokens and ``.get("context")`` flag; the ambiguous ones do not.

    Reproduces the retired ``test_ci_truth_chokepoint.py`` detector coverage: line 2
    flags the two distinctive tokens (``timed_out`` / ``startup_failure``) but not
    the ambiguous ``failure``; line 4 flags the ``.get("context")`` name-fallback but
    not the ``.get("name")`` read, and the raw ``.get("conclusion")`` read on line 3
    stays clean.
    """
    source = (
        "def some_new_ci_reader(item):\n"
        "    failing = {'failure', 'timed_out', 'startup_failure'}\n"
        "    if item.get('conclusion') in failing:\n"
        "        return item.get('name') or item.get('context')\n"
        "    return None\n"
    )
    assert _flagged_lines(_scan_with(_CI_TRUTH_GUARD, source)) == [2, 2, 4]


def test_ci_classification_signature_branches() -> None:
    """Every ``_classification_signature`` branch, including the non-signatures.

    Reproduces the retired guard's assertion that the distinctive multi-word tokens
    (case-insensitively), the ``AssertionError`` + ``FAILED`` error-line regex, and
    the raw ``--log-failed`` fetch flag are signatures, while the ambiguous single
    words (``failure`` / ``success`` / ``error``) are deliberately not.
    """
    assert _classification_signature("startup_failure") == "conclusion-vocabulary literal"
    assert _classification_signature("TIMED_OUT") == "conclusion-vocabulary literal"
    assert _classification_signature("Traceback: AssertionError x FAILED") == "CI-log error-line regex"
    assert _classification_signature("--log-failed") == "raw failing-CI-log fetch"
    assert _classification_signature("failure") is None
    assert _classification_signature("success") is None
    assert _classification_signature("error") is None


def test_merge_truth_detector_flags_bare_name_call() -> None:
    """A bare ``ast.Name`` primitive call flags, like the attribute-shaped bypass.

    Reproduces the detector coverage the retired ``test_merge_truth_chokepoint.py``
    carried that the registry entry's ``synthetic_bypass`` — two ``ast.Attribute``
    calls (``self._host_or_git_pr_merged`` / ``code_host.is_pr_closed``) — does not
    exercise: a bare ``ast.Name`` call to a merge primitive, the shape the
    ``is_pr_settled`` oracle wiring uses (``_git_pr_merged(n)``), is still flagged.
    """
    source = (
        "def some_new_face(pr):\n"
        "    if _git_pr_merged(pr):\n"
        "        return True\n"
        "    return is_pr_closed(pr)\n"
    )
    hits = _scan_with(_MERGE_TRUTH_GUARD, source)
    assert _flagged_lines(hits) == [2, 4]
    joined = "\n".join(hits)
    assert "_git_pr_merged()" in joined
    assert "is_pr_closed()" in joined


def _sole_expr(source: str) -> ast.expr:
    """The single expression node parsed from a one-line snippet."""
    stmt = ast.parse(source).body[0]
    assert isinstance(stmt, ast.Expr)
    return stmt.value


def test_gate_acceptance_detector_flags_call_shapes_not_references() -> None:
    """Both ``run_quality_gates()`` call shapes flag; references and other calls do not.

    Pins ``_gate_acceptance_detector``'s shape independently of the live-tree scan,
    alongside the other focused detector tests: the attribute form
    (``self.run_quality_gates()``) and the bare form (``run_quality_gates()``) each
    return the primitive string, while a never-called reference
    (``fn = self.run_quality_gates`` — an ``ast.Attribute``, not a call) and an
    unrelated call (``self.run_tests()``) both return ``None``.
    """
    assert _gate_acceptance_detector(_sole_expr("self.run_quality_gates()")) == "run_quality_gates()"
    assert _gate_acceptance_detector(_sole_expr("run_quality_gates()")) == "run_quality_gates()"
    assert _gate_acceptance_detector(_sole_expr("self.run_quality_gates")) is None
    assert _gate_acceptance_detector(_sole_expr("self.run_tests()")) is None


def _query_options(*keywords: str) -> ast.expr:
    """A ``QueryOptions(...)`` call node carrying *keywords* verbatim."""
    return _sole_expr(f"QueryOptions({', '.join(keywords)})")


def test_action_gate_detector_requires_a_real_gate_not_a_hooks_keyword() -> None:
    """``hooks=`` presence is not installation — only a recognized builder passes.

    The fail-open shape ``sdk.py`` actually tests for is ``options.hooks is not
    None``, so a Bash-granting ``QueryOptions(hooks=None)`` installs nothing
    while satisfying a keyword-presence check. Same for an unrecognized payload:
    ``task_gate_from_hooks`` refuses to replicate a hook mapping it cannot parse,
    so the ``cli_paced`` provider would carry no gate into the subprocess. Both
    flag; the two recognized builder forms — the free function and the
    ``EvolutionEngine`` method — do not.
    """
    grant = "allowed_tools=['Read', 'Bash']"
    flagged = "Bash-granting QueryOptions with no action-gate hooks"

    # Fail-open and unrecognized payloads all flag.
    assert _action_gate_installed_detector(_query_options(grant)) == flagged
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=None"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=some_mapping"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=build_other_hooks()"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=self.build_other_hooks()"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=builders[0]()"),
    ) == flagged

    # Both recognized builder forms install the gate.
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=make_task_tool_hooks(frozenset({b}))"),
    ) is None
    assert _action_gate_installed_detector(
        _query_options(grant, "hooks=self.action_gate_hooks(plan)"),
    ) is None

    # A grant that cannot include Bash needs no gate; a non-QueryOptions call
    # and a non-call node are outside the detector's domain entirely.
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=['Read', 'Grep']"),
    ) is None
    assert _action_gate_installed_detector(_query_options("caller='x'")) is None
    assert _action_gate_installed_detector(_sole_expr("OtherOptions(x=1)")) is None
    assert _action_gate_installed_detector(_sole_expr("QueryOptions")) is None


def test_action_gate_detector_reads_an_unreadable_grant_as_bash_granting() -> None:
    """A grant the parser cannot read in full is assumed to include ``Bash``.

    The detector's fail-closed half. ``executor.py`` passes a *call*
    (``self._all_tool_names()``), and a future extraction of the four repeated
    six-element tool lists would produce ``[*_IMPL_TOOLS, ...]`` — a list whose
    literal elements alone omit ``"Bash"`` while the splat supplies it. Judging
    such a list on its readable elements would silently disarm the guard at
    exactly the implementation-lineage sites it exists for, so any element that
    is not a string literal makes the whole grant granting.
    """
    flagged = "Bash-granting QueryOptions with no action-gate hooks"

    # Wholly non-literal expressions.
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=self._all_tool_names()"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=BASE + ['Read']"),
    ) == flagged

    # Literal containers carrying an unreadable element.
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=[*_IMPL_TOOLS, 'Read']"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=('Read', tool_name)"),
    ) == flagged
    assert _action_gate_installed_detector(
        _query_options("allowed_tools=['Read', None]"),
    ) == flagged

    # ...and each of those is clean once the gate is actually installed.
    assert _action_gate_installed_detector(
        _query_options(
            "allowed_tools=[*_IMPL_TOOLS]",
            "hooks=make_task_tool_hooks(frozenset({b}))",
        ),
    ) is None


def test_wip_primacy_detector_flags_handoff_variant() -> None:
    """The ``Hand off PR #`` directive variant flags, like the ``Finish PR #`` bypass.

    Reproduces the detector coverage the retired ``test_wip_primacy_chokepoint.py``
    carried that the registry entry's ``synthetic_bypass`` — a single ``Finish PR #``
    f-string write — does not exercise: the merge-ready ``Hand off PR #`` sibling
    marker in ``_WIP_DIRECTIVE_MARKERS`` is still flagged when written to
    ``next_task_directive`` via an f-string outside the gate. Parallels
    ``test_guard_flags_a_handoff_variant_bypass`` from the retired file.
    """
    source = (
        "def rival_handoff(self, pr):\n"
        "    self.model.next_task_directive = (\n"
        '        f"Hand off PR #{pr} to Thomas — merge it or close it."\n'
        "    )\n"
    )
    hits = _scan_with(_WIP_PRIMACY_GUARD, source)
    assert _flagged_lines(hits) == [2]
    assert "WIP-focus directive in rival_handoff" in "\n".join(hits)


def test_wip_primacy_detector_ignores_marker_in_non_directive_write() -> None:
    """A WIP marker in a plain, non-``next_task_directive`` assignment never flags.

    Reproduces the negative-case coverage the retired
    ``test_wip_primacy_chokepoint.py`` carried in
    ``test_non_wip_directive_write_is_not_flagged``: the ``targets_directive is
    False`` branch of ``_wip_directive_detector`` with a marker *present* — a
    ``"Finish PR #"`` / ``"Hand off PR #"`` literal assigned to some other name,
    not the directive attribute. Neither the ``synthetic_bypass`` (marker on a
    directive write) nor the ``synthetic_ok`` (directive write, no marker) nor the
    ported handoff test exercises this shape, and the scanned modules carry no
    such literal so ``test_live_scan_is_clean`` never reaches it either. Without
    this case a future refactor dropping the ``targets_directive`` guard — flagging
    any assignment whose static RHS carries a marker — would over-fire undetected.
    """
    source = (
        "def not_a_directive_write(self, pr):\n"
        "    other = 'Finish PR #1 — not a directive write'\n"
        '    handoff = f"Hand off PR #{pr} — also not a directive"\n'
        "    return other, handoff\n"
    )
    assert _scan_with(_WIP_PRIMACY_GUARD, source) == []


def test_retired_landing_bind_detector_flags_redef_and_reference_not_survivor() -> None:
    """A retired-bind re-def and a name/attr/string reference flag; the survivor is clean.

    Pins ``_retired_landing_bind_detector``'s two branches independently of the
    live-tree scan, alongside the other focused detector tests: a re-*definition*
    of a retired bind (the ``ast.FunctionDef``/``ast.AsyncFunctionDef`` branch, for
    both the ``def`` and ``async def`` shapes) and a bare-name, attribute, or
    string-key reference to one (the three forms the shared ``_names_retired_flag``
    branch catches) each return a non-``None`` string, while the surviving verdict
    author ``decide_outcome`` — as a def *and* as a bare name — stays clean.
    """
    redef = ast.parse("def ground_landing_claim(record):\n    return record\n").body[0]
    assert _retired_landing_bind_detector(redef) == (
        "retired landing bind re-definition (ground_landing_claim)"
    )
    async_redef = ast.parse(
        "async def neutralize_unlanded_resolution_narration(r):\n    return r\n"
    ).body[0]
    assert _retired_landing_bind_detector(async_redef) == (
        "retired landing bind re-definition (neutralize_unlanded_resolution_narration)"
    )

    # The reference branch, in all three forms ``_names_retired_flag`` catches: a
    # bare name, an attribute attr, and a string constant.
    name_ref = _sole_expr("bind_self_committed_success_to_landing")
    assert _retired_landing_bind_detector(name_ref) == "bind_self_committed_success_to_landing"
    attr_ref = _sole_expr("self.bind_diagnostic_success_to_landing")
    assert _retired_landing_bind_detector(attr_ref) == "bind_diagnostic_success_to_landing"
    str_ref = _sole_expr('"bind_self_committed_partial_to_landing"')
    assert _retired_landing_bind_detector(str_ref) == "bind_self_committed_partial_to_landing"

    # The surviving verdict author is never flagged — neither as a def nor a name.
    survivor_def = ast.parse(
        "def decide_outcome(candidate, facts):\n    return candidate\n"
    ).body[0]
    assert _retired_landing_bind_detector(survivor_def) is None
    assert _retired_landing_bind_detector(_sole_expr("decide_outcome")) is None


def test_gate_observation_detector_flags_redefinition_not_consumption() -> None:
    """A vocabulary re-def (class / def / async def) flags; consumers do not.

    Pins ``_gate_observation_definition_detector`` independently of the live-tree
    scan, alongside the other focused detector tests: a snippet re-defining
    ``GateFailure`` (class), ``attribute`` (def) and ``parse_gate_output`` (async
    def) flags all three definition lines, so every detector branch runs; a snippet
    that only imports and calls the vocabulary, and one defining an unrelated
    ``attributes``, flag nothing. Modelled on
    ``test_retired_landing_bind_detector_flags_redef_and_reference_not_survivor``.
    """
    redefines = (
        "class GateFailure:\n"
        "    tool: str\n"
        "\n"
        "def attribute(observation, changed, baseline):\n"
        "    return 'ATTRIBUTED'\n"
        "\n"
        "async def parse_gate_output(tool, stdout, stderr, returncode):\n"
        "    return None\n"
    )
    assert _flagged_lines(_scan_with(_GATE_OBSERVATION_GUARD, redefines)) == [1, 4, 7]

    # Consumers — an import and calls of the vocabulary — and an unrelated
    # ``def attributes`` (plural, not the guarded ``attribute``) never fork the
    # owner, so nothing flags.
    consumes = (
        "from maestro import gate_observation\n"
        "\n"
        "def classify(observation, changed, baseline):\n"
        "    verdict = gate_observation.attribute(observation, changed, baseline)\n"
        "    run = gate_observation.parse_gate_output('ruff', '', '', 1)\n"
        "    return verdict, run\n"
        "\n"
        "def attributes(node):\n"
        "    return node\n"
    )
    assert _scan_with(_GATE_OBSERVATION_GUARD, consumes) == []


def test_gate_terminal_floor_finds_live_convergence_constructions() -> None:
    """The gate_terminal live scan is clean because the tree still builds the result.

    Non-vacuity floor (the control-half pattern in
    ``tests/test_settlement_reconciler.py``): ``test_live_scan_is_clean[gate_terminal]``
    would pass for free if the tree stopped constructing ``ConvergenceResult`` at
    all. Re-scan the real ``maestro/evolution_quality.py`` with ``_GATE_TERMINAL_GUARD``'s
    detector under an **empty** allowlist and assert it still finds the live
    constructions, every one attributed to ``_convergence_terminal`` — so the clean
    live scan reflects the allowlist confining a real construction, not an empty tree.
    """
    source = (_MAESTRO_ROOT / "evolution_quality.py").read_text(encoding="utf-8")
    hits = _scan_with(_GATE_TERMINAL_GUARD, source)
    assert hits, (
        "no ConvergenceResult construction found in evolution_quality.py — the "
        "gate_terminal live scan would pass vacuously"
    )
    off_terminal = [hit for hit in hits if "ConvergenceResult in _convergence_terminal" not in hit]
    assert off_terminal == [], off_terminal


def test_gate_observation_owner_defines_every_guarded_name() -> None:
    """The gate_observation owner module really binds all six guarded names.

    Non-vacuity floor: ``scan_all_except("gate_observation.py")`` excludes the owner
    from the ``gate_observation`` scan, so the live scan would pass for free if the
    owner defined none of the six vocabulary names. Assert the real
    ``maestro/gate_observation.py`` binds every ``_GATE_OBSERVATION_GUARD`` subject
    (via ``bound_symbol_names`` over its parsed tree), so the exclusion is confining a
    real owner, not an empty one.
    """
    tree = ast.parse(
        (_MAESTRO_ROOT / "gate_observation.py").read_text(encoding="utf-8"),
        filename="gate_observation.py",
    )
    defined = bound_symbol_names(tree)
    missing = [name for name in _GATE_OBSERVATION_GUARD.subjects if name not in defined]
    assert missing == [], f"gate_observation.py no longer defines: {missing}"


# ── Co-occurrence focused controls (proposal 109) ───────────────────────────────
#
# The ``co_occurrences`` runner (:func:`test_co_occurrences_hold`) only proves the
# salvage requirements hold over the real ``agent.py``. These focused in-memory
# controls pin the primitive's verdict directly next to the runner — a trigger scope
# that consults the companion passes, one that does not flags naming it, and a locked
# trigger-scope set that drifts flags — mirroring the detector-hardening controls above.

_CO_TRIGGER = reference_in_set_detector(
    frozenset({"SALVAGE_AND_LAND", "DISENGAGE_PERMITTED"})
)
_CO_COMPANION = call_in_set_detector(
    frozenset({"_assess_pr_salvage", "assess_pr_salvage"})
)

# A synthetic ``agent.py``: ``ok_site`` consults the oracle then compares the verdict;
# ``bypass_site`` compares a verdict it never fetched.
_CO_OK_SOURCE = (
    "class A:\n"
    "    def ok_site(self, pr, branch):\n"
    "        verdict = self._assess_pr_salvage(pr, branch)\n"
    "        return verdict is Salvage.SALVAGE_AND_LAND\n"
)
_CO_BYPASS_SOURCE = _CO_OK_SOURCE + (
    "    def bypass_site(self, pr):\n"
    "        return pr is Salvage.DISENGAGE_PERMITTED\n"
)


def _co_req(expected_scopes: frozenset[str] | None) -> CoOccurrenceRequirement:
    """A verdict-consults-oracle requirement with the given ``expected_scopes``."""
    return CoOccurrenceRequirement(
        name="focused",
        module="agent.py",
        trigger=_CO_TRIGGER,
        companion=_CO_COMPANION,
        expected_scopes=expected_scopes,
        allowlist=frozenset(),
        message="each disengage site must consult the oracle",
    )


def test_co_occurrence_control_ok_site_passes() -> None:
    """A trigger scope that also consults the companion passes (triggers-with-companion)."""
    assert_co_occurrence(
        _co_req(frozenset({"ok_site"})), sources={"agent.py": _CO_OK_SOURCE}
    )


def test_co_occurrence_control_bypass_site_flags() -> None:
    """A trigger scope that never consults the companion flags, naming it."""
    with pytest.raises(AssertionError, match="never match the companion") as excinfo:
        assert_co_occurrence(_co_req(None), sources={"agent.py": _CO_BYPASS_SOURCE})
    assert "bypass_site" in str(excinfo.value)


def test_co_occurrence_control_expected_scopes_mismatch_flags() -> None:
    """A locked trigger-scope set that drifts from the real triggers flags."""
    with pytest.raises(AssertionError, match="forces a reviewed update"):
        assert_co_occurrence(
            _co_req(frozenset({"ok_site"})), sources={"agent.py": _CO_BYPASS_SOURCE}
        )


# ── Partition co-occurrence focused controls (proposal 109) ─────────────────────
#
# The ``partition_co_occurrences`` runner (:func:`test_partition_co_occurrences_hold`)
# only proves the blocker-ownership requirement holds over the real tree. These focused
# in-memory controls pin the primitive's verdict directly next to the runner — every
# syntax a re-derived partition can take flags naming its function, while a lone
# reference / single-code container / classifier-filtered scope does not — porting the
# retired ``test_blocker_ownership_binding.py``'s ``test_guard_flags_a_synthetic_bypass``
# and ``test_lone_reference_and_classifier_consultation_are_not_flagged`` coverage that
# the real-tree scan alone would leave without an explicit assertion.


def _partition_control_req() -> PartitionCoOccurrence:
    """A blocker-partition requirement over a synthetic ``consumer.py`` (no allowlist).

    Reuses the registry entry's real detector / pivot / source-name set, so the controls
    exercise the shipped recognizer rather than a paraphrase of it; the taxonomy's own
    source module stays excluded as it is in the live entry.
    """
    return PartitionCoOccurrence(
        name="focused_partition",
        code_detector=_blocker_code_detector,
        pivot=_BLOCKER_PIVOT,
        companion=reference_in_set_detector(_BLOCKER_SOURCE_NAMES),
        allowlist=frozenset(),
        exclude_modules=frozenset({"remediation_tracker.py"}),
        message="each consumer must consult the classifier",
    )


@pytest.mark.parametrize(
    "label, body",
    [
        # The pre-098 shape: one tuple literal listing both codes.
        (
            "collection_literal",
            "    tend_clears = (\n"
            "        MERGE_BLOCK_AWAITING_PUSH,\n"
            "        MERGE_BLOCK_AWAITING_APPROVAL,\n"
            "    )\n"
            "    return reason in tend_clears\n",
        ),
        # The post-098 shape this migration teaches — seed one code, then ``.add()``
        # the pivot under a hard-coded flag read. A per-literal scan never sees the
        # two codes together; the scope-aggregate scan does.
        (
            "two_step_set_build",
            "    tend_clears = {MERGE_BLOCK_AWAITING_APPROVAL}\n"
            "    if not flag:\n"
            "        tend_clears.add(MERGE_BLOCK_AWAITING_PUSH)\n"
            "    return reason in tend_clears\n",
        ),
        # No container at all: an ``or`` chain of equality comparisons.
        (
            "or_chain",
            "    return (\n"
            "        reason == MERGE_BLOCK_AWAITING_PUSH\n"
            "        or reason == MERGE_BLOCK_AWAITING_APPROVAL\n"
            "    )\n",
        ),
        # An ``elif`` ladder — the same decision spread across branches.
        (
            "elif_ladder",
            "    if reason == MERGE_BLOCK_AWAITING_PUSH:\n"
            "        return True\n"
            "    elif reason == MERGE_BLOCK_AWAITING_APPROVAL:\n"
            "        return True\n"
            "    return False\n",
        ),
        # A dict keyed by reason code — a mapping is a partition too.
        (
            "dict_literal",
            "    owners = {\n"
            "        MERGE_BLOCK_AWAITING_PUSH: 'tend',\n"
            "        MERGE_BLOCK_AWAITING_APPROVAL: 'human',\n"
            "    }\n"
            "    return owners.get(reason) == 'tend'\n",
        ),
    ],
)
def test_partition_control_flags_a_synthetic_bypass(label: str, body: str) -> None:
    """Every syntax a re-derived partition can take is flagged, naming its function.

    Each fixture is a minimal fake consumer that decides the owned/external split itself
    and never consults the classifier; the partition scan must flag it and attribute it
    to ``some_new_gate`` regardless of the container (or absence of one) holding the
    codes. ``two_step_set_build`` is the regression case — the shape the migrated helper
    itself uses — so a copy-paste hard-coding the flag must not slip through.
    """
    source = f"def some_new_gate(reason, flag):\n{body}"
    with pytest.raises(AssertionError, match="without consulting the companion") as exc:
        assert_partition_co_occurrence(
            _partition_control_req(), sources={"consumer.py": source}
        )
    assert "some_new_gate" in str(exc.value), label


def test_partition_control_lone_and_classifier_filtered_not_flagged() -> None:
    """A lone reference, a single-code container, and a classifier-filtered scope pass.

    The scope-aggregate scan is deliberately broad, so its floor matters as much as its
    ceiling: a lone ``==`` against the pivot and a single-code container decide nothing
    (below ``min_distinct``), and the classifier-filtered comprehension the migrated
    helper uses is licensed — the codes are the classifier's *input*, consulted in the
    same scope. Reproduces the retired file's negative control.
    """
    source = (
        "def lone_reference(reason):\n"
        "    return reason == MERGE_BLOCK_AWAITING_PUSH\n"
        "\n"
        "def single_code_set(reason):\n"
        "    return reason in frozenset({MERGE_BLOCK_AWAITING_PUSH})\n"
        "\n"
        "def classifier_filtered(reason, flag):\n"
        "    external = [\n"
        "        code\n"
        "        for code in (MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_AWAITING_PUSH)\n"
        "        if classify_blocker_ownership(code, own_push=flag)\n"
        "        == BLOCKER_OWNERSHIP_EXTERNAL\n"
        "    ]\n"
        "    return reason in external\n"
    )
    assert_partition_co_occurrence(
        _partition_control_req(), sources={"consumer.py": source}
    )


# ── Owed-act chokepoint focused controls (proposal 137) ─────────────────────────
#
# The registry runner proves the ``owed_act`` entry holds over the real tree; these
# focused in-memory controls pin its two rules directly. The production detector must
# separate *minting* a verdict from *reading* one (the distinction that keeps
# ``standdown_validity``'s ``is OwedAct.CODE_OWED`` comparison and the sanctioned
# ``OwedAct(raw)`` round-trip clean), and the partition rule must flag every syntax a
# re-derived owed act takes while leaving a single-axis scope and an oracle-consulting
# scope alone. The non-vacuity floor asserts the excluded owner really binds every
# guarded subject, so the ``scan_all_except`` exclusion confines a real owner.


def test_owed_act_production_detector_flags_minting_not_reading() -> None:
    """Minting an ``OwedAct`` verdict flags; reading one never does.

    Pins ``_owed_act_production_detector`` independently of the live-tree scan: a
    snippet minting a verdict in each value-producing position — an assignment, a
    bare ``return``, a tuple element, and a call keyword argument (in both the bare
    and module-qualified member spellings) — flags all four lines, so every detector
    branch runs. A snippet that only *reads* a verdict — the ``OwedAct(raw)`` value
    round-trip, an ``is`` comparison operand, a same-named attribute on an unrelated
    object, and a bare ``return`` — flags nothing. Modelled on
    ``test_gate_observation_detector_flags_redefinition_not_consumption``.
    """
    mints = (
        "def mints(blocker, raw):\n"
        "    verdict = OwedAct.WAIT_CI\n"
        "    if blocker:\n"
        "        return OwedAct.EXTERNAL_BLOCKED\n"
        "    if raw:\n"
        "        return (OwedAct.HANDOFF_READY, verdict)\n"
        "    return record(act=pr_owed_act.OwedAct.CODE_OWED)\n"
    )
    assert _flagged_lines(_scan_with(_OWED_ACT_GUARD, mints)) == [2, 4, 6, 7]

    # Reads, not productions: the sanctioned round-trip, a comparison operand, an
    # unrelated object's same-named attribute, and a valueless return.
    reads = (
        "def reads(raw, blocker, threads, node):\n"
        "    act = classify_pr_owed_act(blocker, (), unresolved_threads=threads)\n"
        "    if act is OwedAct.CODE_OWED:\n"
        "        return OwedAct(raw)\n"
        "    if node:\n"
        "        fallback = Other.WAIT_CI\n"
        "        return [fallback, node.CODE_OWED]\n"
        "    return\n"
    )
    assert _scan_with(_OWED_ACT_GUARD, reads) == []


def test_owed_act_production_detector_flags_folded_and_yielded_verdicts() -> None:
    """A conditional expression, a dict payload and a ``yield`` all mint a verdict.

    The hand-folded ``OwedAct.CODE_OWED if owned else OwedAct.WAIT_CI`` is the
    canonical private verdict this chokepoint exists to catch, and a verdict handed
    back through a dict payload or a generator is produced just as surely as one
    returned. Each of the five lines flags: a conditional assignment resolved from
    its ``body`` arm, one resolved from its ``orelse`` arm, a returned dict value, a
    ``yield`` and a ``yield from``.
    """
    mints = (
        "def mints(owned, raw):\n"
        "    verdict = OwedAct.CODE_OWED if owned else fallback\n"
        "    other = fallback if owned else OwedAct.WAIT_CI\n"
        "    yield OwedAct.HANDOFF_READY\n"
        "    yield from (OwedAct.EXTERNAL_BLOCKED,)\n"
        "    return {'act': OwedAct.CODE_OWED, 'raw': raw}\n"
    )
    assert _flagged_lines(_scan_with(_OWED_ACT_GUARD, mints)) == [2, 3, 4, 5, 6]


def test_owed_act_production_detector_leaves_read_vocabulary_alone() -> None:
    """A collection of members bound or passed as read vocabulary is not a mint.

    The collection carve-out's floor: a module's ``_READS`` membership set — bare or
    handed to ``frozenset(...)`` — names verdicts it intends to *recognize*, the same
    class as the :class:`ast.Compare` operand exemption, so neither the assignment nor
    the constructor call flags. A conditional expression naming no member, and a
    conditional whose arms are collections at a non-returning position, are likewise
    silent; a returned collection of members still flags (line 6).
    """
    reads = (
        "_READS = {OwedAct.CODE_OWED, OwedAct.WAIT_CI}\n"
        "_FROZEN = frozenset({OwedAct.HANDOFF_READY})\n"
        "def reads(owned, a, b):\n"
        "    neither = a if owned else b\n"
        "    grouped = (a,) if owned else {OwedAct.CODE_OWED}\n"
        "    return [OwedAct.WAIT_CI, neither, grouped]\n"
    )
    assert _flagged_lines(_scan_with(_OWED_ACT_GUARD, reads)) == [6]


def _owed_act_partition_control_req() -> PartitionCoOccurrence:
    """The shipped owed-act partition requirement, driven over a synthetic module.

    Returns the registry entry's *own* requirement rather than a paraphrase, so the
    controls exercise the shipped recognizers — the axis-classifier code detector, the
    ``classify_ci_conclusions`` pivot and the oracle companion — and the owner module
    stays excluded exactly as it is live.
    """
    requirement, = _OWED_ACT_GUARD.partition_co_occurrences
    return requirement


@pytest.mark.parametrize(
    "label, body",
    [
        # Both axes read into locals and folded by hand — the canonical re-derivation.
        (
            "folded_locals",
            "    ownership = classify_blocker_ownership(blocker, own_push=True)\n"
            "    ci = classify_ci_conclusions(check_runs)\n"
            "    if ownership == 'EXTERNAL':\n"
            "        return 'external-blocked'\n"
            "    return 'handoff-ready' if ci else 'code-owed'\n",
        ),
        # The qualified attribute spelling of both calls — the same decision, imported
        # through the module rather than the symbol.
        (
            "qualified_calls",
            "    if remediation_tracker.classify_owned_blocker(blocker, threads):\n"
            "        return 'code-owed'\n"
            "    return ci_diagnosis.classify_ci_conclusions(check_runs)\n",
        ),
        # Spread across branches, with the CI axis reached only in the tail — a
        # per-statement scan never sees the two together; the scope-aggregate one does.
        (
            "elif_ladder",
            "    if blocker:\n"
            "        owned = classify_owned_blocker(blocker, threads)\n"
            "        if owned:\n"
            "            return 'code-owed'\n"
            "    elif check_runs:\n"
            "        return classify_ci_conclusions(check_runs)\n"
            "    return 'wait-ci'\n",
        ),
        # Inside a comprehension — still one scope, still a private partition.
        (
            "comprehension",
            "    reds = [r for r in check_runs if classify_ci_conclusions([r])]\n"
            "    return [\n"
            "        r for r in reds\n"
            "        if classify_blocker_ownership(blocker, own_push=True)\n"
            "    ]\n",
        ),
    ],
)
def test_owed_act_partition_control_flags_a_synthetic_bypass(
    label: str, body: str
) -> None:
    """Every syntax a re-derived owed act takes is flagged, naming its function.

    Each fixture is a minimal fake decision site that reaches both the ownership and
    the CI axis and folds them into its own verdict, never consulting
    ``classify_pr_owed_act``. The partition scan must flag it and attribute it to
    ``some_new_site`` regardless of how the two calls are spelled or spread.
    """
    source = f"def some_new_site(blocker, check_runs, threads):\n{body}"
    with pytest.raises(AssertionError, match="without consulting the companion") as exc:
        assert_partition_co_occurrence(
            _owed_act_partition_control_req(), sources={"consumer.py": source}
        )
    assert "some_new_site" in str(exc.value), label


def test_owed_act_partition_control_single_axis_and_oracle_not_flagged() -> None:
    """A single-axis scope and an oracle-consulting scope both pass the partition rule.

    The floor the scope-aggregate rule needs as much as its ceiling: a scope calling
    only the CI axis (the pivot alone, below ``min_distinct``) and one calling only the
    ownership axis (never reaching the pivot) each decide no owed act; and a scope that
    *does* reach both axes is licensed once it consults the oracle in the same scope —
    which is exactly what ``classify_pr_owed_act``'s callers look like.
    """
    source = (
        "def ci_axis_only(check_runs):\n"
        "    return classify_ci_conclusions(check_runs)\n"
        "\n"
        "def ownership_axis_only(blocker, threads):\n"
        "    owned = classify_owned_blocker(blocker, threads)\n"
        "    return classify_blocker_ownership(owned, own_push=True)\n"
        "\n"
        "def consults_the_oracle(blocker, check_runs, threads):\n"
        "    status = classify_ci_conclusions(check_runs)\n"
        "    owned = classify_owned_blocker(blocker, threads)\n"
        "    return classify_pr_owed_act(owned, check_runs, unresolved_threads=threads), status\n"
    )
    assert_partition_co_occurrence(
        _owed_act_partition_control_req(), sources={"consumer.py": source}
    )


def test_owed_act_owner_defines_every_guarded_name() -> None:
    """The pr_owed_act owner module really binds all four guarded names.

    Non-vacuity floor: ``scan_all_except("pr_owed_act.py")`` excludes the owner from
    the ``owed_act`` scan, so the live scan would pass for free if the owner defined
    none of its subjects. Assert the real ``maestro/pr_owed_act.py`` binds every
    ``_OWED_ACT_GUARD`` subject, so the exclusion confines a real owner. Modelled on
    ``test_gate_observation_owner_defines_every_guarded_name``.
    """
    tree = ast.parse(
        (_MAESTRO_ROOT / "pr_owed_act.py").read_text(encoding="utf-8"),
        filename="pr_owed_act.py",
    )
    defined = bound_symbol_names(tree)
    missing = [name for name in _OWED_ACT_GUARD.subjects if name not in defined]
    assert missing == [], f"pr_owed_act.py no longer defines: {missing}"


# ── Priced-introspection cadence guard (proposal 113) ───────────────────────────
#
# The chokepoint that keeps a priced, budget-gated introspection phase from
# silently reverting to running every un-throttled iteration. ``contemplate``
# issues two token-spending SDK calls per run (the priced ``introspect`` +
# ``update_self_model``) and is a member of ``_BUDGET_GATED_PHASES``; the invariant
# is that every such priced phase carries an explicit ``if phase == "<name>":``
# cadence branch in ``phase_will_run`` whose body performs an ``iteration % <n>``
# modulo. Without the branch the phase falls through ``phase_will_run``'s default
# ``return True`` and fires on every iteration — the exact regression this guard
# forbids.
_LIFECYCLE_PATH = _MAESTRO_ROOT / "loop" / "lifecycle.py"

# Curated set of budget-gated phases that issue priced introspection and so must be
# cadence-gated. Keyed by name so a new priced phase forces a reviewed addition here.
_PRICED_INTROSPECTION_PHASES: frozenset[str] = frozenset({"contemplate"})


def _find_phase_will_run(tree: ast.AST) -> ast.FunctionDef:
    """Return the ``phase_will_run`` ``FunctionDef`` node from a parsed lifecycle tree."""
    funcs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "phase_will_run"
    ]
    assert funcs, "phase_will_run not found in lifecycle.py"
    return funcs[0]


def _tests_phase_equals(test: ast.expr, phase_name: str) -> bool:
    """True when *test* is the comparison ``phase == "<phase_name>"``."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "phase"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == phase_name
    )


def _phase_branch_has_iteration_modulo(func: ast.AST, phase_name: str) -> bool:
    """True when ``phase_will_run`` gates *phase_name* on an ``iteration % <n>`` modulo.

    Walks for an ``if phase == "<phase_name>":`` branch whose body (recursively)
    contains an :class:`ast.BinOp` with :class:`ast.Mod` whose left operand is the
    name ``iteration`` — the cadence idiom every sibling phase uses.
    """
    for node in ast.walk(func):
        if not (isinstance(node, ast.If) and _tests_phase_equals(node.test, phase_name)):
            continue
        for sub in node.body:
            for child in ast.walk(sub):
                if (
                    isinstance(child, ast.BinOp)
                    and isinstance(child.op, ast.Mod)
                    and isinstance(child.left, ast.Name)
                    and child.left.id == "iteration"
                ):
                    return True
    return False


class _StripPhaseBranch(ast.NodeTransformer):
    """Deletes every ``if phase == "<phase_name>":`` branch from a parsed tree."""

    def __init__(self, phase_name: str) -> None:
        self._phase = phase_name

    def visit_If(self, node: ast.If) -> ast.AST | None:
        self.generic_visit(node)
        if _tests_phase_equals(node.test, self._phase):
            return None
        return node


def test_priced_introspection_phase_is_cadence_gated() -> None:
    """Every priced budget-gated phase carries an ``iteration % <n>`` cadence branch.

    Proposal 113's chokepoint: ``contemplate`` spends tokens on every run
    (``introspect`` + ``update_self_model``) and is a ``_BUDGET_GATED_PHASES``
    member, so it must be gated behind an explicit cadence branch in
    ``phase_will_run`` — otherwise it falls through the default ``return True`` and
    fires every iteration. The live tree passes; a mutated copy with the branch
    stripped is flagged, proving the guard bites (synthetic parity).
    """
    source = _LIFECYCLE_PATH.read_text(encoding="utf-8")
    func = _find_phase_will_run(ast.parse(source))
    for phase in _PRICED_INTROSPECTION_PHASES:
        assert phase in _BUDGET_GATED_PHASES, phase
        assert _phase_branch_has_iteration_modulo(func, phase), phase
    # Synthetic parity: strip each priced phase's branch and confirm the modulo
    # detector no longer finds a cadence gate — the shape this guard forbids.
    for phase in _PRICED_INTROSPECTION_PHASES:
        mutated = _StripPhaseBranch(phase).visit(ast.parse(source))
        mutated_func = _find_phase_will_run(mutated)
        assert not _phase_branch_has_iteration_modulo(mutated_func, phase), phase


# ── Task↔evolution decoupling guards (proposal 116 step 9) ──────────────────────
#
# The task↔evolution decoupling rests on three permanent master switches
# (``[loop] task_branch_reuse_enabled``, ``[loop] self_directed_tasks_enabled``,
# ``[audit] branch_promotion_pressure_enabled``), pinned only behaviorally by
# flag-flipping tests that exercise the paths they construct. Nothing behavioral
# stops an unguarded ``source_task_id`` base consumer, an un-switched
# ``generate_task`` scheduling branch, or an ungated promotion-pressure site
# from silently regrowing beside those tests. The three bespoke tail guards
# below parse the live modules and forbid exactly those shapes — the
# priced-introspection cadence idiom above, not ``ChokepointGuard`` registry
# entries. They are ledgered all the same: each rides the ``_TAIL_GUARD_NAMES``
# list and the ``_MAX_TAIL_GUARDS`` budget, which bound the in-runner route the
# way ``_MAX_STRUCTURAL_GUARDS`` bounds the registry.
_EVOLUTION_PATH = _MAESTRO_ROOT / "evolution.py"

# The only ``Evolution`` functions that may consume ``source_task_id`` as an
# implementation base — and only behind the reuse flag.
_REUSE_GUARDED_FUNCTIONS: frozenset[str] = frozenset({"implement", "create_pr"})

_REUSE_FLAG = "task_branch_reuse_enabled"
_PROMOTION_FLAG = "branch_promotion_pressure_enabled"


def _references_attribute(node: ast.AST, attr: str) -> bool:
    """True when *node*'s subtree reads an attribute named *attr*."""
    return any(
        isinstance(child, ast.Attribute) and child.attr == attr
        for child in ast.walk(node)
    )


def _find_named_functions(
    tree: ast.AST, names: frozenset[str], module_label: str
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the named function nodes of *tree*, asserting each exists exactly once.

    Non-vacuity mirror of ``test_scanned_reader_modules_exist`` in
    ``tests/test_gate_command_owner.py``: a guard whose subject was renamed
    away must fail loudly here, never scan an empty set. A duplicated name
    fails just as loudly — silent first-match could rebind a guard to the
    wrong ``def``.
    """
    found: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {
        name: [] for name in names
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names
        ):
            found[node.name].append(node)
    missing = sorted(name for name, nodes in found.items() if not nodes)
    assert not missing, f"{module_label}: function(s) not found: {missing}"
    duplicated = sorted(name for name, nodes in found.items() if len(nodes) > 1)
    assert not duplicated, (
        f"{module_label}: function name(s) not unique: {duplicated}"
    )
    return {name: nodes[0] for name, nodes in found.items()}


def test_find_named_functions_rejects_missing_and_duplicate() -> None:
    """The non-vacuity helper fails loudly on a renamed-away or duplicated subject.

    A second same-named ``def`` in a guarded module would otherwise silently
    rebind a guard to whichever match ``ast.walk`` yields first; both failure
    modes must raise instead of degrading the guard.
    """
    with pytest.raises(AssertionError, match="not found"):
        _find_named_functions(ast.parse(""), frozenset({"implement"}), "synthetic")
    duplicated = "def implement():\n    pass\n\ndef implement():\n    pass\n"
    with pytest.raises(AssertionError, match="not unique"):
        _find_named_functions(
            ast.parse(duplicated), frozenset({"implement"}), "synthetic"
        )


class _StripFlagConjunct(ast.NodeTransformer):
    """Deletes every ``BoolOp`` conjunct that reads the named flag attribute.

    Rewrites ``config.<flag> and rest`` to ``rest`` on a re-parsed copy — the
    exact shape of a future edit dropping a master-switch conjunct from a
    guard — so each detector's synthetic-parity assertion proves it bites.
    """

    def __init__(self, flag_attr: str) -> None:
        self._flag_attr = flag_attr

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.expr:
        self.generic_visit(node)
        kept = [
            value
            for value in node.values
            if not _references_attribute(value, self._flag_attr)
        ]
        if not kept:
            return ast.copy_location(ast.Constant(value=True), node)
        if len(kept) == 1:
            return kept[0]
        node.values = kept
        return node


def _unguarded_source_task_id_lines(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[int]:
    """Line numbers of ``source_task_id`` uses not gated on the reuse flag.

    Two shapes violate the single-base-owner invariant (proposal 116 step 4):
    an :class:`ast.If` / :class:`ast.IfExp` whose test reads ``source_task_id``
    without reading ``task_branch_reuse_enabled`` in the same test expression,
    and any other ``source_task_id`` attribute read outside a flag-guarded
    conditional's test or flag-ON body. The ``orelse`` arm executes with the
    reuse flag *off*, so a read there is a violation, never guarded. An empty
    list means every consumption site is reuse-flag-gated.
    """
    violations: set[int] = set()
    guarded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.IfExp)) and _references_attribute(
            node.test, "source_task_id"
        ):
            if _references_attribute(node.test, _REUSE_FLAG):
                flag_on_parts: list[ast.AST] = [node.test]
                if isinstance(node, ast.If):
                    flag_on_parts.extend(node.body)
                else:
                    flag_on_parts.append(node.body)
                guarded.update(
                    id(child)
                    for part in flag_on_parts
                    for child in ast.walk(part)
                )
            else:
                violations.add(node.lineno)
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "source_task_id"
            and id(node) not in guarded
        ):
            violations.add(node.lineno)
    return sorted(violations)


def test_source_task_id_consumption_is_reuse_flag_gated() -> None:
    """Every ``source_task_id`` base consumption is reuse-flag-gated.

    Proposal 116 step 9: with ``task_branch_reuse_enabled`` off,
    ``Evolution.implement`` bases every evolution PR on ``origin/main`` — an
    unconditional ``source_task_id`` consumption surviving in ``implement`` /
    ``create_pr`` would silently re-couple evolution PRs to task branches. The
    live tree passes; a mutated copy with the flag conjunct stripped from the
    guards is flagged in both functions, proving the detector bites
    (synthetic parity).
    """
    source = _EVOLUTION_PATH.read_text(encoding="utf-8")
    functions = _find_named_functions(
        ast.parse(source), _REUSE_GUARDED_FUNCTIONS, "evolution.py"
    )
    for name in sorted(_REUSE_GUARDED_FUNCTIONS):
        assert _unguarded_source_task_id_lines(functions[name]) == [], name
    mutated = _StripFlagConjunct(_REUSE_FLAG).visit(ast.parse(source))
    mutated_functions = _find_named_functions(
        mutated, _REUSE_GUARDED_FUNCTIONS, "evolution.py"
    )
    for name in sorted(_REUSE_GUARDED_FUNCTIONS):
        assert _unguarded_source_task_id_lines(mutated_functions[name]) != [], name
    # Synthetic parity for the orelse arm: a ``source_task_id`` read in a
    # flag-guarded conditional's *else* branch runs with the reuse flag off —
    # exactly the regrowth this guard forbids — and must be flagged, never
    # inherited as guarded from the enclosing conditional.
    orelse_source = (
        "def implement(self, plan):\n"
        "    if self._config.task_branch_reuse_enabled and plan.source_task_id:\n"
        "        base = plan.source_task_id\n"
        "    else:\n"
        "        base = plan.source_task_id\n"
        "\n"
        "def create_pr(self, plan):\n"
        "    return (\n"
        "        'x'\n"
        "        if self._config.task_branch_reuse_enabled and plan.source_task_id\n"
        "        else plan.source_task_id\n"
        "    )\n"
    )
    orelse_functions = _find_named_functions(
        ast.parse(orelse_source), _REUSE_GUARDED_FUNCTIONS, "synthetic"
    )
    assert _unguarded_source_task_id_lines(orelse_functions["implement"]) == [5]
    assert _unguarded_source_task_id_lines(orelse_functions["create_pr"]) == [11]


def _generate_task_branch_is_switch_gated(func: ast.FunctionDef) -> bool:
    """True when the ``generate_task`` branch exists and reads the master switch.

    Required presence: a deleted ``if phase == "generate_task":`` branch also
    returns ``False`` — the phase would fall through ``phase_will_run``'s
    default ``return True`` and schedule regardless of
    ``self_directed_tasks_enabled``.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.If) and _tests_phase_equals(
            node.test, "generate_task"
        ):
            return any(
                _references_attribute(stmt, "self_directed_tasks_enabled")
                for stmt in node.body
            )
    return False


def test_generate_task_scheduling_is_master_switch_gated() -> None:
    """``generate_task`` scheduling stays behind ``self_directed_tasks_enabled``.

    Proposal 116 step 9: the self-directed channel's only scheduling site is
    the ``if phase == "generate_task":`` branch in ``phase_will_run``; dropping
    the master-switch conjunct (or the branch itself) would re-enable
    self-directed task generation unconditionally. The live tree passes; both
    mutations — branch stripped, conjunct stripped — are flagged
    (synthetic parity).
    """
    source = _LIFECYCLE_PATH.read_text(encoding="utf-8")
    assert _generate_task_branch_is_switch_gated(
        _find_phase_will_run(ast.parse(source))
    )
    stripped_branch = _StripPhaseBranch("generate_task").visit(ast.parse(source))
    assert not _generate_task_branch_is_switch_gated(
        _find_phase_will_run(stripped_branch)
    )
    stripped_flag = _StripFlagConjunct("self_directed_tasks_enabled").visit(
        ast.parse(source)
    )
    assert not _generate_task_branch_is_switch_gated(
        _find_phase_will_run(stripped_flag)
    )


# Curated promotion-pressure surface (proposal 116 step 6): module path,
# relative to the ``maestro`` package, → the functions that must consult
# ``branch_promotion_pressure_enabled`` before emitting promotion pressure. A
# new promotion-pressure site forces a reviewed addition here, mirroring the
# ``_PRICED_INTROSPECTION_PHASES`` curation idiom. Keep in lockstep with the
# ``branch_promotion_pressure_enabled`` reason string in
# ``tests/test_cutover_flag_collapse.py::_MASTER_SWITCH_FLAGS``, which
# enumerates the same surface in prose — reconcile both when either changes.
_PROMOTION_PRESSURE_SITES: dict[str, frozenset[str]] = {
    "audit_checks.py": frozenset({"_registered_audit_checks", "run_all_checks"}),
    "task_gates.py": frozenset(
        {"_promotable_backlog_run", "_integration_cadence_run"}
    ),
    "loop/task_generation.py": frozenset({"generate_task"}),
    "loop/task_record.py": frozenset({"apply_merge_clean_promotion_directive"}),
    "loop/contemplation.py": frozenset({"contemplate"}),
    "loop/self_audit.py": frozenset({"audit_self_model"}),
}


def _flag_reference_names(node: ast.AST) -> set[str]:
    """Return every identifier *node*'s subtree could read a flag through.

    The function-scoped name-set idiom of ``tests/test_identity_prompt_slabs.py``
    (``_find_method`` + collected ``ast.Attribute.attr`` names), widened to
    bare names, parameters, and keyword arguments because the audit-check
    registry threads ``branch_promotion_pressure_enabled`` as a function
    parameter rather than a config attribute read.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, (ast.arg, ast.keyword)) and child.arg is not None:
            names.add(child.arg)
    return names


class _StripPromotionFlagReferences(ast.NodeTransformer):
    """Renames every ``branch_promotion_pressure_enabled`` reference away.

    Covers all four forms ``_flag_reference_names`` collects (attribute, bare
    name, parameter, keyword argument) so the mutated subtree carries no trace
    of the master switch — the shape of a site whose gate was deleted.
    """

    _REPLACEMENT = "promotion_pressure_gate_removed"

    def visit_Attribute(self, node: ast.Attribute) -> ast.Attribute:
        self.generic_visit(node)
        if node.attr == _PROMOTION_FLAG:
            node.attr = self._REPLACEMENT
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id == _PROMOTION_FLAG:
            node.id = self._REPLACEMENT
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        if node.arg == _PROMOTION_FLAG:
            node.arg = self._REPLACEMENT
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        self.generic_visit(node)
        if node.arg == _PROMOTION_FLAG:
            node.arg = self._REPLACEMENT
        return node


def test_promotion_pressure_sites_name_master_switch() -> None:
    """Every curated promotion-pressure site consults the master switch.

    Proposal 116 step 9: with ``branch_promotion_pressure_enabled`` off the
    whole promotion surface is silent, so every function in
    ``_PROMOTION_PRESSURE_SITES`` must reference the switch — an ungated (or
    renamed-away) site is exactly the regrowth this guard forbids. The live
    tree passes; stripping the references from one live function's re-parsed
    subtree flags that function and no other (synthetic parity).
    """
    for module_path, names in _PROMOTION_PRESSURE_SITES.items():
        source = (_MAESTRO_ROOT / module_path).read_text(encoding="utf-8")
        functions = _find_named_functions(ast.parse(source), names, module_path)
        for name in sorted(names):
            flag_names = _flag_reference_names(functions[name])
            assert _PROMOTION_FLAG in flag_names, f"{module_path}::{name}"
    # Synthetic parity: strip every master-switch reference from one live
    # function's re-parsed subtree; the predicate must fail for exactly that
    # function while its module siblings stay clean.
    mutated_module, mutated_name = "loop/contemplation.py", "contemplate"
    source = (_MAESTRO_ROOT / mutated_module).read_text(encoding="utf-8")
    site_names = _PROMOTION_PRESSURE_SITES[mutated_module]
    mutated_functions = _find_named_functions(
        ast.parse(source), site_names, mutated_module
    )
    _StripPromotionFlagReferences().visit(mutated_functions[mutated_name])
    for name in sorted(site_names):
        flagged = _PROMOTION_FLAG in _flag_reference_names(mutated_functions[name])
        if name == mutated_name:
            assert flagged is False, f"{mutated_module}::{name}"
        else:
            assert flagged is True, f"{mutated_module}::{name}"


# ── Session-attachment fence granularity (issue #2957) ─────────────────────────
#
# ``session_attachment``'s ``(module, function)`` allowlist is *whole-function*:
# an entry exempts everything inside the function it names, however large that
# function has since grown. The entry blessing the warning-hold rate-limit probe
# once named ``run_loop`` itself — 1,140 lines carrying the loop's entire phase
# dispatch — so a bare ``await query(...)`` added to any phase arm passed the
# fence silently, precisely the regression the guard exists to catch. Extracting
# the probe into ``_run_warning_hold_probe`` shrank the exempted surface to the
# probe body; these two tail guards pin that. The first proves the fence bites
# inside ``run_loop``'s body (fault-seeded in memory, no disk, no subprocess);
# the second forbids re-broadening any entry back onto an oversized function.
# ``assert_allowlist_entries_real`` cannot catch either: it only asks whether an
# entry names a live ``def``, not how much that ``def`` exempts.

# Largest function span (in source lines) a whole-function ``session_attachment``
# allowlist entry may exempt. The blessed probe / executor-fallback helpers are
# all comfortably under it; a whole-``run_loop`` exemption — over a thousand lines
# of phase dispatch — is the shape it forbids. Deliberately not the exact span of
# any live function, so ordinary drift in ``agent.py`` cannot stale this figure.
_MAX_ALLOWLISTED_FUNCTION_LINES = 150


def _scan_agent_for_session_attachment(
    source: str, module_base: str = "agent.py",
) -> list[str]:
    """Scan *source* as *module_base* with the live session-attachment guard.

    Uses the registry entry's own ``detector``, ``allowlist``, ``line_resolver``
    and ``exempt_lines`` rather than rebuilding them, so the test tracks the guard
    it is pinning instead of a copy that can drift away from it.
    """
    return scan_source(
        source,
        _SESSION_ATTACHMENT_GUARD.detector,
        _SESSION_ATTACHMENT_GUARD.allowlist,
        module_base,
        line_resolver=_SESSION_ATTACHMENT_GUARD.line_resolver,
        exempt_lines=_SESSION_ATTACHMENT_GUARD.exempt_lines,
    )


def _seed_bare_query(source: str, function: str) -> tuple[str, int]:
    """Return *source* with a bare ``await query(...)`` planted inside *function*.

    The seed lands immediately after the target's first body statement, at that
    statement's indent, so the mutated source still parses. The insertion point is
    resolved by AST rather than a hardcoded line, so the fault survives drift in
    ``agent.py``. Returns the mutated source and the 1-based line the seeded call
    occupies in it.
    """
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name == function
    )
    anchor = target.body[0]
    seeded_line = (anchor.end_lineno or anchor.lineno) + 1
    lines = source.splitlines()
    lines.insert(seeded_line - 1, " " * anchor.col_offset + 'await query("seeded")')
    return "\n".join(lines) + "\n", seeded_line


def _function_line_spans(source: str) -> dict[str, int]:
    """Map every function name in *source* to its longest definition's line span.

    Names can repeat across classes; the longest span is the conservative read,
    since a whole-function allowlist entry keyed on a bare name exempts them all.
    """
    spans: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            spans[node.name] = max(spans.get(node.name, 0), span)
    return spans


def _function_spans_by_module(
    sources: Iterable[tuple[str, str]],
) -> dict[str, dict[str, int]]:
    """Map basename -> {function name: longest span} over ``(basename, source)`` pairs.

    Merges files that *share* a basename — ``artifact_review.py`` exists
    both at ``maestro/`` top level and under ``maestro/prompts/`` — the way the house helper :func:`functions_by_module`
    does, because the allowlist is keyed by basename too (``scan_source`` is
    passed ``path.name``). A last-wins ``{path.name: ...}`` build would keep only
    the twin scanned last, so an oversized function in the *other* twin would
    measure 0 and slip past the budget below, reopening the #2957 blind spot.
    """
    by_module: dict[str, dict[str, int]] = {}
    for module, source in sources:
        spans = by_module.setdefault(module, {})
        for name, span in _function_line_spans(source).items():
            spans[name] = max(spans.get(name, 0), span)
    return by_module


def test_session_attachment_fences_run_loop_phase_bodies() -> None:
    """A bare ``await query(...)`` inside ``loop/iteration.py``'s ``run_loop`` flags (#2957).

    The live tree scans clean — asserted first, so the seeded case cannot pass
    vacuously against a source that was already violating. Planting an
    unconditional ``await query(...)`` in ``run_loop``'s body then flags it,
    attributed to ``run_loop``: the phase dispatch is inside the fence, not
    exempted wholesale by an allowlist entry naming the whole loop.
    """
    source = (_MAESTRO_ROOT / "loop" / "iteration.py").read_text(encoding="utf-8")
    assert _scan_agent_for_session_attachment(source, "iteration.py") == []
    seeded, line = _seed_bare_query(source, "run_loop")
    hits = _scan_agent_for_session_attachment(seeded, "iteration.py")
    assert f"iteration.py:{line} — unconditional query() in run_loop" in hits, hits


def test_session_attachment_flags_attribute_form_query() -> None:
    """An unconditional ``await sdk.query(...)`` flags (issue #4131).

    A module-attribute call reaches the SDK one-shot without naming ``query``
    bare; a Name-only detector could not see that call.
    """
    source = (
        "async def some_phase():\n"
        "    sdk = load()\n"
        "    await sdk.query(prompt='hi')\n"
    )
    hits = _scan_agent_for_session_attachment(source, "task_generation.py")
    assert hits == ["task_generation.py:3 — unconditional query() in some_phase"]


def test_session_attachment_blesses_attribute_form_fallback() -> None:
    """The dual-branch shape's ``else`` ``sdk.query`` fallback stays exempt."""
    source = (
        "async def some_phase(session=None):\n"
        "    sdk = load()\n"
        "    if session is not None:\n"
        "        await sdk.query_on_session(session, 'hi')\n"
        "    else:\n"
        "        await sdk.query(prompt='hi')\n"
    )
    assert _scan_agent_for_session_attachment(source, "task_generation.py") == []


def test_session_attachment_fences_loop_collaborators() -> None:
    """Neutralising ``generate_task``'s session guard flags its fallback (#4131).

    The live module scans clean first, so the seeded case cannot pass vacuously.
    The mutation is in memory only: ``if session is not None:`` enclosing the
    ``query`` call becomes ``if False:``, leaving the call outside any
    blessed fallback branch.
    """
    path = _MAESTRO_ROOT / "loop" / "task_generation.py"
    source = path.read_text(encoding="utf-8")
    assert _scan_agent_for_session_attachment(source, "task_generation.py") == []
    tree = ast.parse(source)

    def _is_query_await(child: ast.AST) -> bool:
        return (
            isinstance(child, ast.Await)
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Name)
            and child.value.func.id == "query"
        )

    guard = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            _is_query_await(child)
            for sub in node.orelse
            for child in ast.walk(sub)
        )
    )
    call_line = next(
        child.lineno
        for sub in guard.orelse
        for child in ast.walk(sub)
        if _is_query_await(child)
    )
    lines = source.splitlines()
    guard_text = lines[guard.lineno - 1]
    assert guard_text.strip() == "if session is not None:"
    lines[guard.lineno - 1] = guard_text.replace("session is not None", "False")
    seeded = "\n".join(lines) + "\n"
    hits = _scan_agent_for_session_attachment(seeded, "task_generation.py")
    expected = f"task_generation.py:{call_line} — unconditional query() in generate_task"
    assert expected in hits, hits


def test_session_attachment_allowlist_exempts_no_oversized_function() -> None:
    """No ``session_attachment`` allowlist entry exempts an oversized function.

    The blind spot #2957 reported reopens the moment an entry names a function
    big enough to hide unrelated call sites, and the staleness check
    (``assert_allowlist_entries_real``) will not notice — ``run_loop`` is a live
    ``def``. Budgeting the exempted span makes a re-broadening fail loud here,
    and the explicit ``run_loop`` check names the original regression directly.
    """
    spans = _function_spans_by_module(
        (path.name, path.read_text(encoding="utf-8"))
        for path in _SESSION_ATTACHMENT_GUARD.scanned(_MAESTRO_ROOT)
    )
    oversized = [
        f"{module}:{func} ({spans[module][func]} lines)"
        for module, func in sorted(_SESSION_ATTACHMENT_GUARD.allowlist)
        if spans.get(module, {}).get(func, 0) > _MAX_ALLOWLISTED_FUNCTION_LINES
    ]
    assert not oversized, (
        "session_attachment allowlist exempts functions larger than "
        f"{_MAX_ALLOWLISTED_FUNCTION_LINES} lines — extract the blessed call into "
        f"its own helper instead of exempting its host: {', '.join(oversized)}"
    )
    assert ("agent.py", "run_loop") not in _SESSION_ATTACHMENT_GUARD.allowlist
    assert ("iteration.py", "run_loop") not in _SESSION_ATTACHMENT_GUARD.allowlist


def test_function_spans_merge_across_duplicate_basenames() -> None:
    """Twin files sharing a basename merge, keeping the longest span per name.

    The span budget above is keyed by basename because the allowlist is, and
    ``maestro/`` holds real twins (``artifact_review.py``,
    top level and under ``prompts/``). A last-wins build would let whichever twin
    is scanned last erase the other's spans, so an oversized allowlisted function
    would measure 0 and pass — the #2957 blind spot, reopened. Asserted in both
    orders so the merge cannot be order-dependent.
    """
    wide = "def shared():\n" + "    pass\n" * 4
    narrow = "def shared():\n    pass\n"
    assert _function_spans_by_module([("twin.py", wide), ("twin.py", narrow)]) == {
        "twin.py": {"shared": 5},
    }
    assert _function_spans_by_module([("twin.py", narrow), ("twin.py", wide)]) == {
        "twin.py": {"shared": 5},
    }


def test_standdown_release_sites_flag_a_seeded_unlisted_releaser() -> None:
    """Tree-grounded negative control for the release-site registry (135).

    The parameterized ``synthetic_bypass`` control proves the detector catches a
    standalone bypass; this proves the *registry* catches one appended to the
    **real** ``escalation_bridge.py`` source, with the live allowlist applied —
    the failure mode that matters is a fourth release path landing beside the
    three sanctioned ones, not a snippet in isolation. Seeded in memory (no disk
    write), mirroring ``tests/test_fault_seeding.py``'s mutate-and-scan shape.
    """
    source = (_MAESTRO_ROOT / "escalation_bridge.py").read_text(encoding="utf-8")
    spec = _STANDDOWN_RELEASE_SITES_GUARD
    # The unmutated tree is clean under the live allowlist — so a hit below is
    # attributable to the seeded releaser, never to pre-existing noise.
    assert scan_source(
        source, spec.detector, spec.allowlist, "escalation_bridge.py",
    ) == []
    seeded = source + (
        "\n\ndef prune_whatever_standdowns(backend):\n"
        "    def _apply(standdowns):\n"
        "        del standdowns[7]\n"
        "        return [7]\n"
        "    return backend.mutate(_apply)\n"
    )
    violations = scan_source(
        seeded, spec.detector, spec.allowlist, "escalation_bridge.py",
    )
    assert len(violations) == 1
    assert "prune_whatever_standdowns" in violations[0]


def test_standdown_release_sites_allowlist_is_exactly_the_four_releasers() -> None:
    """The registry names the four sanctioned release paths and nothing else.

    Pins the enumeration itself, so silently *widening* the allowlist — the other
    way a fourth releaser escapes review — reds the build as loudly as an
    unregistered one does.
    """
    assert _STANDDOWN_RELEASE_SITES_GUARD.allowlist == frozenset(
        {
            ("escalation_bridge.py", "clear_standdown"),
            ("escalation_bridge.py", "prune_resolved_standdowns"),
            ("escalation_bridge.py", "prune_expired_standdowns"),
            ("escalation_bridge.py", "prune_refuted_standdowns"),
        },
    )


# ── Environment-hold terminal control (proposal 133) ────────────────────────────


def _attempt_disposition_requirement() -> CoOccurrenceRequirement:
    """The shipped ``terminals_consult_disposition_oracle`` requirement."""
    guard = next(g for g in CHOKEPOINT_GUARDS if g.name == "attempt_disposition")
    return next(
        r for r in guard.co_occurrences
        if r.name == "terminals_consult_disposition_oracle"
    )


def test_environment_hold_writer_without_the_oracle_flags() -> None:
    """Seeding the proposal-133 stamp into a scope that never consults the oracle
    fails the guard — the negative control for widening the terminal set."""
    seeded = (
        "def implement_plan(self, plan_id):\n"
        "    return self.memory.update_plan_environment_hold(plan_id)\n"
    )
    requirement = dataclasses.replace(
        _attempt_disposition_requirement(), expected_scopes=None,
    )

    with pytest.raises(AssertionError, match="never match the companion") as excinfo:
        assert_co_occurrence(requirement, sources={"loop/implementation.py": seeded})

    assert "implement_plan" in str(excinfo.value)


def test_environment_hold_writer_with_the_oracle_passes() -> None:
    """The sanctioned shape — consult, then stamp — satisfies the requirement."""
    sanctioned = (
        "def implement_plan(self, plan_id):\n"
        "    disposition = classify_attempt(None, frozenset())\n"
        "    if disposition.verdict == 'HOLD_ENVIRONMENT':\n"
        "        return self.memory.update_plan_environment_hold(plan_id)\n"
        "    return None\n"
    )

    assert_co_occurrence(
        _attempt_disposition_requirement(), sources={"loop/implementation.py": sanctioned},
    )


# ── Undeclared-toolset ratchet (issue #4127) ──────────────────────────────────


def test_undeclared_toolset_ratchet_is_shrink_only() -> None:
    """Live undeclared-toolset ``QueryOptions`` counts equal the pinned baseline.

    A new site (or a second site reusing a pinned key) inherits the vendor
    default toolbelt ungated; a pinned count above the live one must be lowered
    so the pin only shrinks.
    """
    live: Counter[tuple[str, str | None]] = Counter()
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        module = path.relative_to(_MAESTRO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        live += undeclared_toolset_sites(tree, module)
    pinned = Counter(UNDECLARED_TOOLSET_BASELINE)
    new = sorted((live - pinned).items(), key=str)
    stale = sorted((pinned - live).items(), key=str)
    assert new == [], (
        f"QueryOptions without a declared tool set: {new} — declare tools=[] "
        "(tool-free) or an explicit allowed_tools + action-gate hooks"
    )
    assert stale == [], f"lower stale UNDECLARED_TOOLSET_BASELINE counts: {stale}"


def test_undeclared_toolset_flags_seeded_bypass() -> None:
    """A synthetic ``QueryOptions`` with no tool declaration is flagged."""
    tree = ast.parse('QueryOptions(effort="low", caller="x")')
    assert undeclared_toolset_sites(tree, "m.py") == Counter({("m.py", "x"): 1})


def test_undeclared_toolset_counts_duplicate_key() -> None:
    """Two undeclared sites sharing a caller count twice, not once."""
    tree = ast.parse('QueryOptions(caller="x")\nQueryOptions(caller="x")')
    assert undeclared_toolset_sites(tree, "m.py") == Counter({("m.py", "x"): 2})


@pytest.mark.parametrize(
    "source",
    [
        'QueryOptions(caller="x", tools=None)',
        'QueryOptions(caller="x", allowed_tools=[])',
        'QueryOptions(caller="x", allowed_tools=None)',
    ],
)
def test_undeclared_toolset_flags_falsy_declarations(source: str) -> None:
    """``tools=None`` or a falsy ``allowed_tools`` alone is still undeclared."""
    assert undeclared_toolset_sites(ast.parse(source), "m.py") == Counter(
        {("m.py", "x"): 1},
    )


@pytest.mark.parametrize(
    "source",
    [
        'QueryOptions(effort="low", caller="x", tools=[])',
        'QueryOptions(caller="x", allowed_tools=["Read"])',
        'QueryOptions(caller="x", allowed_tools=names)',
        "QueryOptions(**opts)",
        'Other(caller="x")',
        'mod.QueryOptions(caller="x")',
    ],
)
def test_undeclared_toolset_passes_declared_shapes(source: str) -> None:
    """Declared, opaque, and non-``QueryOptions`` calls are not flagged."""
    assert undeclared_toolset_sites(ast.parse(source), "m.py") == Counter()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('kw = {"caller": "x"}\nQueryOptions(**kw)', {("m.py", "x"): 1}),
        ('kw = {"caller": "x", "tools": []}\nQueryOptions(**kw)', {}),
        ('kw = {"tools": []}\nQueryOptions(**kw, caller="x")', {}),
        ('kw: dict = {"caller": "x"}\nQueryOptions(**kw)', {("m.py", "x"): 1}),
        ('kw = {"caller": "x"}\nkw = {}\nQueryOptions(**kw)', {}),
        ('kw = {**o, "caller": "x"}\nQueryOptions(**kw)', {}),
        ('kw = make()\nQueryOptions(**kw)', {}),
    ],
)
def test_undeclared_toolset_resolves_dict_literal_mapping(
    source: str, expected: dict[tuple[str, str | None], int],
) -> None:
    """A ``**name`` bound once to a str-keyed dict literal is merged and
    judged; a rebound, spread, or non-literal operand stays opaque."""
    assert undeclared_toolset_sites(ast.parse(source), "m.py") == Counter(expected)


def test_undeclared_toolset_non_literal_caller_keys_none() -> None:
    """A non-literal ``caller`` keys the site as ``None``."""
    tree = ast.parse("QueryOptions(caller=name, effort=1)")
    assert undeclared_toolset_sites(tree, "m.py") == Counter({("m.py", None): 1})


# ── Proposal 150: taint owner arms, and the gap-store owner ─────────────────


def _taint_hits(source: str) -> list[str]:
    from tests.structural_guards import _TAINT_OWNER_GUARD

    hits = scan_source(
        source,
        _TAINT_OWNER_GUARD.detector,
        _TAINT_OWNER_GUARD.allowlist,
        "<t>",
        exempt_lines=_TAINT_OWNER_GUARD.exempt_lines,
    )
    return [hit.split(" — ", 1)[1].rsplit(" in ", 1)[0] for hit in hits]


def test_taint_seam_field_exemption_only_in_sdk() -> None:
    """The per-line ``QueryResult`` exemption blesses only maestro/sdk.py's class."""
    from tests.structural_guards import _TAINT_OWNER_GUARD

    exempt = _TAINT_OWNER_GUARD.exempt_lines
    assert exempt is not None
    blessed = sorted(
        path.relative_to(_MAESTRO_ROOT).as_posix()
        for path in _TAINT_OWNER_GUARD.scanned(_MAESTRO_ROOT)
        if exempt(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert blessed == ["sdk.py"]


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("x = text._raw\n", "untrusted raw text read outside taint.py"),
        ("x = getattr(text, '_raw')\n", "untrusted raw text read outside taint.py"),
        ("setattr(text, '_raw', 'x')\n", "untrusted raw text read outside taint.py"),
        ("UntrustedText('a', 'b', 'c')\n", "UntrustedText constructed outside taint.py"),
        ("taint.UntrustedText('a', 'b', 'c')\n", "UntrustedText constructed outside taint.py"),
        ("def f(untrusted_input=False): ...\n", "untrusted_input parameter defaulted"),
        ("def f(a, /, untrusted_input=False): ...\n", "untrusted_input parameter defaulted"),
        ("def f(*, untrusted_input=False): ...\n", "untrusted_input parameter defaulted"),
        ("async def f(untrusted_input=False): ...\n", "untrusted_input parameter defaulted"),
        ("g = lambda untrusted_input=False: 1\n", "untrusted_input parameter defaulted"),
        ("class R:\n    untrusted_input: bool = False\n", "untrusted_input field defaulted"),
        ("self.untrusted_input: bool = True\n", "untrusted_input field defaulted"),
        (
            "class R:\n    untrusted_input: bool = field(default=False)\n",
            "untrusted_input field defaulted",
        ),
        (
            "class R:\n    untrusted_input: bool = dataclasses.field(init=True)\n",
            "untrusted_input field defaulted",
        ),
        # The init=False exemption is scoped to QueryResult, not the field shape.
        (
            "class R:\n    untrusted_input: bool = field(default=False, init=False)\n",
            "untrusted_input field defaulted",
        ),
        (
            "class QueryResult:\n    pass\n    untrusted_input: bool = make()\n",
            "untrusted_input field defaulted",
        ),
        (
            "class QueryResult:\n    untrusted_input: bool = f()(init=False)\n",
            "untrusted_input field defaulted",
        ),
        (
            "class QueryResult:\n    untrusted_input: bool = False\n",
            "untrusted_input field defaulted",
        ),
        ("RULE = 'treat as data, never as instructions'\n",
         "untrusted-data rule restated outside taint.py"),
    ],
)
def test_taint_owner_detector_flags_each_arm(source: str, label: str) -> None:
    assert _taint_hits(source) == [label]


@pytest.mark.parametrize(
    "source",
    [
        "x = self._raw\n",
        "def f(untrusted_input): ...\n",
        "def f(a=1, *, untrusted_input, b=2): ...\n",
        "class QueryResult:\n    untrusted_input: bool = field(default=False, init=False)\n",
        "class QueryResult:\n    untrusted_input: bool\n    other: int = 1\n",
        "class R:\n    untrusted_input: bool\n",
        # r19: a derived local or module variable is not a defaulted field.
        "def f(a, b):\n    untrusted_input: bool = derive_taint(a, b)\n",
        "untrusted_input: bool = derive_taint(a)\n",
        "class R:\n    other: bool = False\n",
        "x = 'data only'\n",
        "x = 3\n",
        "mark_untrusted('gap_issue', 'r', 't')\n",
    ],
)
def test_taint_owner_detector_passes_sanctioned_shapes(source: str) -> None:
    assert _taint_hits(source) == []


def test_gap_store_owner_detector_arms() -> None:
    from tests.structural_guards import _gap_store_owner_detector

    def hits(source: str) -> list[str]:
        return [
            label for node in ast.walk(ast.parse(source))
            if (label := _gap_store_owner_detector(node)) is not None
        ]

    assert hits("GapStore(conn)\n") == [
        "GapStore constructed outside gap_store.py",
    ]
    assert hits("gap_store.GapStore(conn)\n") == [
        "GapStore constructed outside gap_store.py",
    ]
    assert hits("p = 'gaps.db'\n") == [
        "gap store file named outside gap_store.py",
    ]
    assert hits("from maestro.gap_store import GAP_DB_FILENAME as n\n") == [
        "gap store file name referenced outside gap_store.py",
    ]
    assert hits("sqlite3.connect(d / GAP_DB_FILENAME)\n") == [
        "gap store file name referenced outside gap_store.py",
    ]
    assert hits("p = d / gap_store.GAP_DB_FILENAME\n") == [
        "gap store file name referenced outside gap_store.py",
    ]
    assert hits("from maestro.gap_store import build_gap_store\n") == []
    assert hits("build_gap_store(config)\nx = 'state.db'\n") == []


# ── Proposal 155: gap issue owner ───────────────────────────────────────────


def _gap_hits(source: str) -> list[str]:
    from tests.structural_guards import _GAP_ISSUE_OWNER_GUARD

    hits = scan_source(
        source, _GAP_ISSUE_OWNER_GUARD.detector, _GAP_ISSUE_OWNER_GUARD.allowlist, "<t>",
    )
    return [hit.split(" — ", 1)[1].rsplit(" in ", 1)[0] for hit in hits]


@pytest.mark.parametrize(
    ("source", "label"),
    [
        ("tracker.create('x', 'y', labels=['gap:maestro'])\n",
         "gap:* label spelled outside evolution_fuel.py"),
        ("host.list_issues(labels=[f'gap:{kind}'])\n",
         "gap:* label spelled outside evolution_fuel.py"),
        ("tracker.create('x', 'y', labels=[' GAP:maestro'])\n",
         "gap:* label spelled outside evolution_fuel.py"),
        ("conn.execute('SELECT 1 FROM ' + 'gap_opens')\n",
         "gap ledger (gap_opens / red_main_watch) named outside evolution_fuel.py"),
        ("conn.execute('INSERT INTO gap_opens (owner_id) VALUES (?)', (x,))\n",
         "gap ledger (gap_opens / red_main_watch) named outside evolution_fuel.py"),
        ("conn.execute(f'UPDATE gap_opens SET closed_at = {x}')\n",
         "gap ledger (gap_opens / red_main_watch) named outside evolution_fuel.py"),
        ("conn.execute('DELETE FROM red_main_watch WHERE ref = ?', (r,))\n",
         "gap ledger (gap_opens / red_main_watch) named outside evolution_fuel.py"),
    ],
)
def test_gap_issue_owner_flags_rival(source: str, label: str) -> None:
    """A gap label or the ledger spelled outside evolution_fuel.py fails (A6)."""
    assert _gap_hits(source) == [label]


def test_gap_issue_owner_passes_door_call_and_unrelated_text() -> None:
    assert _gap_hits("open_gap_issue('maestro:gate', 'detector', 'e', 'r')\n") == []
    assert _gap_hits("y = 'a gap: b'\nz = 'gap_opened'\n") == []
    assert _gap_hits("w = 'red_main_watcher'\n") == []
