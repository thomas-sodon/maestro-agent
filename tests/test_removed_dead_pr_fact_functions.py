"""Regression guard: the removed/relocated PR-fact code stays that way.

Proposal 106 consolidates the scattered PR-fact modules. This deterministic,
zero-LLM-cost source scan — the same ``pathlib``-over-``maestro/`` guard family
as ``tests/test_pr_open_regression.py`` — pins eight of its outcomes so a future
edit cannot silently undo them. It reads the real source files, so it touches no
``mem_fs``, network, or subprocess.

**Removed dead functions.** Proposal 106 deleted two functions that had gone
dead as the surrounding logic evolved and that no production path ever called:

* ``blocker_greenness.is_strictly_greener`` — a thin wrapper over
  ``greenness_delta``, which is the function the live PR-churn sensor calls
  directly; and
* ``owned_branches.push_already_discharged`` — a head-at-remote probe with no
  production caller, whose only routing consumer (the ``PushDeliveryRoute.HANDOFF``
  branch of ``resolve_push_delivery_marker``) was itself unreachable and removed
  in the same change.

Both carried dedicated test files that were deleted with them, so a future edit
could silently reintroduce a definition with no test noticing; the removed-def
scan pins that neither ``def`` reappears in shipping code.

**Relocated staleness fact.** Proposal 106 also folded ``maestro/pr_staleness.py``
into ``maestro/pr_facts.py``, moving the grounded ``PrStaleness`` fact and its
``observe_pr_staleness`` observer to that shared home. The single-home scan pins
that the retired module stays gone and that each symbol is defined in exactly one
place — ``pr_facts.py`` — so the module cannot silently reappear and the fact
cannot end up with two drifting copies.

**Relocated severity algebra.** Proposal 106 also folded
``maestro/blocker_greenness.py`` into ``maestro/pr_facts.py``, moving the
blocker-severity total order and its pure ``greenness_delta`` function to that
shared home. The single-home scan pins that the retired module stays gone and
that ``greenness_delta`` is defined in exactly one place — ``pr_facts.py`` — so
the module cannot silently reappear and the function cannot end up with two
drifting copies.

**Relocated merge-truth fact.** Proposal 106 also folded
``maestro/world_state.py`` into ``maestro/pr_facts.py``, moving the frozen
``MergeTruth`` fact and its single fetch-first ``observe_pr_merge_truth``
observer to that shared home. The single-home scan pins that the retired module
stays gone and that each symbol is defined in exactly one place —
``pr_facts.py`` — so the module cannot silently reappear and the fact cannot end
up with two drifting copies.

**Relocated live-plan projection.** Proposal 106 also folded
``maestro/live_work.py`` into ``maestro/pr_facts.py``, moving the live-work oracle
— the frozen ``LivePlan`` view, its ``select_live_plans`` projector, and the
``LIVE_STATUSES`` live-status owner — to that shared home. The single-home scan
pins that the retired module stays gone and that each symbol is defined in exactly
one place — ``pr_facts.py`` — so the module cannot silently reappear and the
projection cannot end up with two drifting copies.

**Relocated owned-branch oracle.** Proposal 106 also folded
``maestro/owned_branches.py`` into ``maestro/pr_facts.py``, moving the two pure
owned-writable-branch oracles — ``compute_owned_writable_branches`` (the branches
the task executor may push to) and ``verify_push_advanced_remote_head`` (whether a
push physically advanced ``origin/<branch>``) — to that shared home. The
single-home scan pins that the retired module stays gone and that each function is
defined in exactly one place — ``pr_facts.py`` — so the module cannot silently
reappear and the oracle cannot end up with two drifting copies.

**Relocated readiness fact.** Proposal 106 also folded ``maestro/pr_readiness.py``
into ``maestro/pr_facts.py``, moving the aggregated ``PRReadiness`` fact, its
single pure ``observe_pr_readiness`` observer, and the ``ci_state_from_conclusion``
CI-state mapper to that shared home — the last stray oracle module and the final
stamped-fact split. The single-home scan pins that the retired module stays gone
and that each symbol is defined in exactly one place — ``pr_facts.py`` — so the
module cannot silently reappear and the fact cannot end up with two drifting
copies. Unlike its siblings, this section anchors its ``def``/``class`` patterns at
column 0 (``^``, ``re.MULTILINE``): the pure ``observe_pr_readiness`` observer
shares its name with the public ``MaestroAgent.observe_pr_readiness`` chokepoint
*method*, so a ``\\b``-only pattern would count the indented method in ``agent.py``
as a second home; anchoring at column 0 pins the module-level definitions and
excludes the method (the negative control below proves it).

**Relocated salvage oracle.** Proposal 106 also folded ``maestro/pr_salvage.py``
into ``maestro/pr_facts.py``, moving the salvage-before-disengage oracle — the
``Salvage`` verdict, its pure ``assess_pr_salvage`` decision core, and the
``classify_pr_disposability`` predicate — to that shared home, the last oracle
module to fold in and the completion of proposal 106's oracle consolidation. The
single-home scan pins that the retired module stays gone and that each symbol is
defined in exactly one place — ``pr_facts.py`` — so the module cannot silently
reappear and the oracle cannot end up with two drifting copies.
"""

from __future__ import annotations

import re
from pathlib import Path

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"

#: The definitions proposal 106 removed. Each pattern matches a ``def`` (plain or
#: ``async def``, since ``\bdef`` sits at the boundary after ``async ``) that
#: reintroduces the dead symbol; the trailing ``\b`` keeps a longer name that
#: merely starts with one of these from false-matching.
_REMOVED_DEFS = (
    re.compile(r"\bdef\s+is_strictly_greener\b"),
    re.compile(r"\bdef\s+push_already_discharged\b"),
)


def _iter_py_files() -> list[Path]:
    return [
        p
        for p in _MAESTRO_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _scan_for_removed_defs(sources: dict[str, str]) -> dict[str, list[str]]:
    """Return ``{relpath: [matched pattern, ...]}`` for reintroduced dead defs.

    Pure over its *sources* mapping (relative path -> file text), so the real-file
    scan and the seeded negative control below share one definition of
    "reintroduced". A source matching none contributes no entry.
    """
    offenders: dict[str, list[str]] = {}
    for rel, source in sources.items():
        hits = [pattern.pattern for pattern in _REMOVED_DEFS if pattern.search(source)]
        if hits:
            offenders[rel] = hits
    return offenders


def test_removed_pr_fact_functions_stay_absent() -> None:
    """Neither removed dead function is redefined anywhere under ``maestro/``."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the absence assertion pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    offenders = _scan_for_removed_defs(sources)
    assert offenders == {}, (
        "Proposal 106 removed these dead PR-fact functions; a shipping module "
        f"reintroduced one: {offenders}. Delete the redefinition — the live "
        "siblings (greenness_delta, and the two wired owned-branch oracles "
        "compute_owned_writable_branches / verify_push_advanced_remote_head) "
        "cover every real caller."
    )


def test_scan_flags_a_seeded_redefinition() -> None:
    """Negative control: a seeded redefinition of either dead def is flagged.

    Without this, a broken pattern (matching nothing) would make the clean-tree
    assertion above pass vacuously. Hands in-memory sources to the pure scan, so
    it touches no filesystem — and exercises both the plain ``def`` and the
    ``async def`` boundary as well as the clean-source path.
    """
    seeded = {
        "blocker_greenness.py": "def is_strictly_greener(a, b):\n    return True\n",
        "owned_branches.py": (
            "async def push_already_discharged(branch):\n    return False\n"
        ),
        "clean.py": "def greenness_delta(before, after):\n    return 0\n",
    }
    offenders = _scan_for_removed_defs(seeded)
    assert offenders == {
        "blocker_greenness.py": [r"\bdef\s+is_strictly_greener\b"],
        "owned_branches.py": [r"\bdef\s+push_already_discharged\b"],
    }


# --- Relocated staleness fact: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after its contents folded into ``pr_facts.py``.
_RETIRED_MODULE = "pr_staleness.py"

#: The single module that must define the relocated staleness fact.
_STALENESS_HOME = "pr_facts.py"

#: The staleness-fact definitions and the ``def``/``class`` pattern that finds
#: each one. The trailing ``\b`` keeps a longer name that merely starts with one
#: of these (``PrStalenessX`` / ``observe_pr_staleness_v2``) from false-matching.
_STALENESS_DEFS = {
    "class PrStaleness": re.compile(r"\bclass\s+PrStaleness\b"),
    "def observe_pr_staleness": re.compile(r"\bdef\s+observe_pr_staleness\b"),
}


def _definition_sites(sources: dict[str, str], pattern: re.Pattern[str]) -> set[str]:
    """Return the set of relpaths in *sources* whose text matches *pattern*.

    Pure over its *sources* mapping (relative path -> file text), so the real-file
    scan and the seeded negative control below share one definition of "defines
    this symbol". A source matching none contributes nothing.
    """
    return {rel for rel, source in sources.items() if pattern.search(source)}


def test_retired_pr_staleness_module_is_absent() -> None:
    """``maestro/pr_staleness.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_MODULE).exists(), (
        "Proposal 106 folded pr_staleness.py into pr_facts.py; the retired module "
        "reappeared. Delete it — the grounded PrStaleness fact and its "
        "observe_pr_staleness observer live in pr_facts.py."
    )


def test_staleness_fact_defined_only_in_pr_facts() -> None:
    """``PrStaleness`` / ``observe_pr_staleness`` are defined only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _STALENESS_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_STALENESS_HOME}, (
            f"Proposal 106 homes the staleness fact in {_STALENESS_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the fact must have exactly one home."
        )


def test_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises both the ``class`` and ``def`` patterns
    across a match, a second match, and a non-matching source.
    """
    seeded = {
        "pr_facts.py": (
            "@dataclass(frozen=True)\nclass PrStaleness:\n    branch: str\n\n"
            "def observe_pr_staleness(branch):\n    return None\n"
        ),
        # A drifting second copy the single-home test must reject.
        "pr_staleness.py": (
            "class PrStaleness:\n    branch: str\n\n"
            "def observe_pr_staleness(branch):\n    return None\n"
        ),
        # A source mentioning the fact only by reference must not count as a home.
        "evolution.py": "from maestro.pr_facts import PrStaleness  # noqa\n",
    }
    assert _definition_sites(seeded, _STALENESS_DEFS["class PrStaleness"]) == {
        "pr_facts.py",
        "pr_staleness.py",
    }
    assert _definition_sites(seeded, _STALENESS_DEFS["def observe_pr_staleness"]) == {
        "pr_facts.py",
        "pr_staleness.py",
    }


# --- Relocated severity algebra: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after its contents folded into ``pr_facts.py``.
_RETIRED_GREENNESS_MODULE = "blocker_greenness.py"

#: The single module that must define the relocated severity algebra.
_GREENNESS_HOME = "pr_facts.py"

#: The severity-algebra definition and the ``def`` pattern that finds it. The
#: trailing ``\b`` keeps a longer name that merely starts with it
#: (``greenness_delta_v2``) from false-matching.
_GREENNESS_DEFS = {
    "def greenness_delta": re.compile(r"\bdef\s+greenness_delta\b"),
}


def test_retired_blocker_greenness_module_is_absent() -> None:
    """``maestro/blocker_greenness.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_GREENNESS_MODULE).exists(), (
        "Proposal 106 folded blocker_greenness.py into pr_facts.py; the retired "
        "module reappeared. Delete it — the blocker-severity total order and its "
        "greenness_delta function live in pr_facts.py."
    )


def test_greenness_algebra_defined_only_in_pr_facts() -> None:
    """``greenness_delta`` is defined only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _GREENNESS_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_GREENNESS_HOME}, (
            f"Proposal 106 homes the severity algebra in {_GREENNESS_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the function must have exactly one home."
        )


def test_greenness_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises the ``def`` pattern across a match, a
    second match, and a non-matching source.
    """
    seeded = {
        "pr_facts.py": "def greenness_delta(before, after):\n    return 0\n",
        # A drifting second copy the single-home test must reject.
        "blocker_greenness.py": "def greenness_delta(before, after):\n    return 0\n",
        # A source mentioning the function only by reference must not count as a home.
        "remediation_tracker.py": (
            "from maestro.pr_facts import greenness_delta  # noqa\n"
        ),
    }
    assert _definition_sites(seeded, _GREENNESS_DEFS["def greenness_delta"]) == {
        "pr_facts.py",
        "blocker_greenness.py",
    }


# --- Relocated merge-truth fact: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after the merge-truth fact folded into ``pr_facts.py``.
_RETIRED_WORLD_STATE_MODULE = "world_state.py"

#: The single module that must define the relocated merge-truth fact.
_MERGE_TRUTH_HOME = "pr_facts.py"

#: The merge-truth definitions and the ``class``/``def`` pattern that finds each
#: one. The trailing ``\b`` keeps a longer name that merely starts with one of
#: these (``MergeTruthV2`` / ``observe_pr_merge_truth_v2``) from false-matching.
_MERGE_TRUTH_DEFS = {
    "class MergeTruth": re.compile(r"\bclass\s+MergeTruth\b"),
    "def observe_pr_merge_truth": re.compile(r"\bdef\s+observe_pr_merge_truth\b"),
}


def test_retired_world_state_module_is_absent() -> None:
    """``maestro/world_state.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_WORLD_STATE_MODULE).exists(), (
        "Proposal 106 folded world_state.py into pr_facts.py; the retired module "
        "reappeared. Delete it — the frozen MergeTruth fact and its "
        "observe_pr_merge_truth observer live in pr_facts.py."
    )


def test_merge_truth_fact_defined_only_in_pr_facts() -> None:
    """``MergeTruth`` / ``observe_pr_merge_truth`` are defined only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _MERGE_TRUTH_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_MERGE_TRUTH_HOME}, (
            f"Proposal 106 homes the merge-truth fact in {_MERGE_TRUTH_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the fact must have exactly one home."
        )


def test_merge_truth_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises both the ``class`` and ``def`` patterns
    across a match, a second match, and a non-matching source.
    """
    seeded = {
        "pr_facts.py": (
            "@dataclass(frozen=True)\nclass MergeTruth(TimestampedObservation):\n"
            "    pr_number: int\n\n"
            "def observe_pr_merge_truth(pr_number):\n    return None\n"
        ),
        # A drifting second copy the single-home test must reject.
        "world_state.py": (
            "class MergeTruth(TimestampedObservation):\n    pr_number: int\n\n"
            "def observe_pr_merge_truth(pr_number):\n    return None\n"
        ),
        # A source mentioning the fact only by reference must not count as a home.
        "agent.py": (
            "from maestro.pr_facts import MergeTruth, observe_pr_merge_truth  # noqa\n"
        ),
    }
    assert _definition_sites(seeded, _MERGE_TRUTH_DEFS["class MergeTruth"]) == {
        "pr_facts.py",
        "world_state.py",
    }
    assert _definition_sites(
        seeded, _MERGE_TRUTH_DEFS["def observe_pr_merge_truth"]
    ) == {
        "pr_facts.py",
        "world_state.py",
    }


# --- Relocated live-plan projection: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after the live-plan projection folded into ``pr_facts.py``.
_RETIRED_LIVE_WORK_MODULE = "live_work.py"

#: The single module that must define the relocated live-plan projection.
_LIVE_WORK_HOME = "pr_facts.py"

#: The live-plan-projection definitions and the ``class``/``def``/assignment
#: pattern that finds each one. The trailing ``\b`` keeps a longer name that merely
#: starts with one of these (``LivePlanV2`` / ``select_live_plans_v2`` /
#: ``LIVE_STATUSES_LEGACY``) from false-matching; the ``LIVE_STATUSES`` pattern
#: keys on the annotated-assignment shape (``LIVE_STATUSES: tuple``) so an
#: ``import`` or membership reference is never mistaken for a definition.
_LIVE_WORK_DEFS = {
    "class LivePlan": re.compile(r"\bclass\s+LivePlan\b"),
    "def select_live_plans": re.compile(r"\bdef\s+select_live_plans\b"),
    "LIVE_STATUSES assignment": re.compile(r"\bLIVE_STATUSES\s*:\s*tuple\b"),
}


def test_retired_live_work_module_is_absent() -> None:
    """``maestro/live_work.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_LIVE_WORK_MODULE).exists(), (
        "Proposal 106 folded live_work.py into pr_facts.py; the retired module "
        "reappeared. Delete it — the LivePlan projection, its select_live_plans "
        "oracle, and the LIVE_STATUSES owner live in pr_facts.py."
    )


def test_live_work_projection_defined_only_in_pr_facts() -> None:
    """``LivePlan`` / ``select_live_plans`` / ``LIVE_STATUSES`` live only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _LIVE_WORK_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_LIVE_WORK_HOME}, (
            f"Proposal 106 homes the live-plan projection in {_LIVE_WORK_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the projection must have exactly one home."
        )


def test_live_work_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises the ``class``, ``def``, and
    ``LIVE_STATUSES`` assignment patterns across a match, a second match, and a
    non-matching source.
    """
    seeded = {
        "pr_facts.py": (
            "LIVE_STATUSES: tuple[LiveStatus, ...] = (\"pending_review\",)\n\n"
            "@dataclass(frozen=True)\nclass LivePlan:\n    plan_id: str\n\n"
            "def select_live_plans(plans):\n    return []\n"
        ),
        # A drifting second copy the single-home test must reject.
        "live_work.py": (
            "LIVE_STATUSES: tuple = (\"pending_review\",)\n\n"
            "class LivePlan:\n    plan_id: str\n\n"
            "def select_live_plans(plans):\n    return []\n"
        ),
        # A source mentioning the projection only by reference must not count as a home.
        "memory.py": (
            "from maestro.pr_facts import LIVE_STATUSES, LivePlan, select_live_plans  # noqa\n"
        ),
    }
    assert _definition_sites(seeded, _LIVE_WORK_DEFS["class LivePlan"]) == {
        "pr_facts.py",
        "live_work.py",
    }
    assert _definition_sites(seeded, _LIVE_WORK_DEFS["def select_live_plans"]) == {
        "pr_facts.py",
        "live_work.py",
    }
    assert _definition_sites(
        seeded, _LIVE_WORK_DEFS["LIVE_STATUSES assignment"]
    ) == {
        "pr_facts.py",
        "live_work.py",
    }


# --- Relocated owned-branch oracle: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after the owned-branch oracle folded into ``pr_facts.py``.
_RETIRED_OWNED_BRANCHES_MODULE = "owned_branches.py"

#: The single module that must define the relocated owned-branch oracle.
_OWNED_BRANCH_HOME = "pr_facts.py"

#: The owned-branch-oracle definitions and the ``def`` pattern that finds each
#: one. The trailing ``\b`` keeps a longer name that merely starts with one of
#: these (``compute_owned_writable_branches_v2`` /
#: ``verify_push_advanced_remote_head_v2``) from false-matching.
_OWNED_BRANCH_DEFS = {
    "def compute_owned_writable_branches": re.compile(
        r"\bdef\s+compute_owned_writable_branches\b"
    ),
    "def verify_push_advanced_remote_head": re.compile(
        r"\bdef\s+verify_push_advanced_remote_head\b"
    ),
}


def test_retired_owned_branches_module_is_absent() -> None:
    """``maestro/owned_branches.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_OWNED_BRANCHES_MODULE).exists(), (
        "Proposal 106 folded owned_branches.py into pr_facts.py; the retired "
        "module reappeared. Delete it — the compute_owned_writable_branches and "
        "verify_push_advanced_remote_head oracles live in pr_facts.py."
    )


def test_owned_branch_oracle_defined_only_in_pr_facts() -> None:
    """The two owned-branch oracles are defined only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _OWNED_BRANCH_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_OWNED_BRANCH_HOME}, (
            f"Proposal 106 homes the owned-branch oracle in {_OWNED_BRANCH_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the oracle must have exactly one home."
        )


def test_owned_branch_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises both ``def`` patterns across a match, a
    second match, and a non-matching source.
    """
    seeded = {
        "pr_facts.py": (
            "def compute_owned_writable_branches(current, open_prs, ledger):\n"
            "    return frozenset()\n\n"
            "def verify_push_advanced_remote_head(branch, sha, *, fetch, read):\n"
            "    return False\n"
        ),
        # A drifting second copy the single-home test must reject.
        "owned_branches.py": (
            "def compute_owned_writable_branches(current, open_prs, ledger):\n"
            "    return frozenset()\n\n"
            "def verify_push_advanced_remote_head(branch, sha, *, fetch, read):\n"
            "    return False\n"
        ),
        # A source mentioning the oracle only by reference must not count as a home.
        "agent.py": (
            "from maestro.pr_facts import compute_owned_writable_branches  # noqa\n"
        ),
    }
    assert _definition_sites(
        seeded, _OWNED_BRANCH_DEFS["def compute_owned_writable_branches"]
    ) == {
        "pr_facts.py",
        "owned_branches.py",
    }
    assert _definition_sites(
        seeded, _OWNED_BRANCH_DEFS["def verify_push_advanced_remote_head"]
    ) == {
        "pr_facts.py",
        "owned_branches.py",
    }


# --- Relocated readiness fact: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after the aggregated readiness fact folded into ``pr_facts.py``.
_RETIRED_PR_READINESS_MODULE = "pr_readiness.py"

#: The single module that must define the relocated readiness fact.
_PR_READINESS_HOME = "pr_facts.py"

#: The readiness-fact definitions and the ``class``/``def`` pattern that finds
#: each one. Anchored at column 0 (``^``, ``re.MULTILINE``) rather than with a
#: leading ``\b`` like the sibling sections: the pure ``observe_pr_readiness``
#: observer shares its name with the public ``MaestroAgent.observe_pr_readiness``
#: chokepoint method, so a ``\b`` pattern would count that indented method in
#: ``agent.py`` as a second home. Anchoring at column 0 matches only the
#: module-level definitions; the trailing ``\b`` still keeps a longer name that
#: merely starts with one of these (``PRReadinessV2`` / ``observe_pr_readiness_v2``
#: / ``ci_state_from_conclusion_v2``) from false-matching.
_PR_READINESS_DEFS = {
    "class PRReadiness": re.compile(r"^class\s+PRReadiness\b", re.MULTILINE),
    "def observe_pr_readiness": re.compile(
        r"^def\s+observe_pr_readiness\b", re.MULTILINE
    ),
    "def ci_state_from_conclusion": re.compile(
        r"^def\s+ci_state_from_conclusion\b", re.MULTILINE
    ),
}


def test_retired_pr_readiness_module_is_absent() -> None:
    """``maestro/pr_readiness.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_PR_READINESS_MODULE).exists(), (
        "Proposal 106 folded pr_readiness.py into pr_facts.py; the retired module "
        "reappeared. Delete it — the aggregated PRReadiness fact, its "
        "observe_pr_readiness observer, and the ci_state_from_conclusion mapper "
        "live in pr_facts.py."
    )


def test_readiness_fact_defined_only_in_pr_facts() -> None:
    """``PRReadiness`` / ``observe_pr_readiness`` / ``ci_state_from_conclusion`` live only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _PR_READINESS_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_PR_READINESS_HOME}, (
            f"Proposal 106 homes the readiness fact in {_PR_READINESS_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the fact must have exactly one home."
        )


def test_readiness_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home, excludes the chokepoint method.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises the ``class`` and both ``def`` patterns
    across a match, a drifting second match, and — crucially — a source (`agent.py`)
    whose *indented* ``MaestroAgent.observe_pr_readiness`` method the column-0
    anchor must NOT count as a definition home.
    """
    seeded = {
        "pr_facts.py": (
            "@dataclass(frozen=True)\nclass PRReadiness(TimestampedObservation):\n"
            "    pr_number: int\n\n"
            'def ci_state_from_conclusion(conclusion):\n    return "PENDING"\n\n'
            "def observe_pr_readiness(pr_number):\n    return None\n"
        ),
        # A drifting second copy the single-home test must reject.
        "pr_readiness.py": (
            "class PRReadiness(TimestampedObservation):\n    pr_number: int\n\n"
            'def ci_state_from_conclusion(conclusion):\n    return "PENDING"\n\n'
            "def observe_pr_readiness(pr_number):\n    return None\n"
        ),
        # The MaestroAgent.observe_pr_readiness chokepoint method is indented, so
        # the column-0 anchor must NOT count agent.py as a definition home — this
        # is why this section anchors ``^def`` where its siblings use ``\b``.
        "agent.py": (
            "class MaestroAgent:\n"
            "    def observe_pr_readiness(self, pr_num):\n"
            "        return _observe_pr_readiness(pr_num)\n"
        ),
    }
    assert _definition_sites(seeded, _PR_READINESS_DEFS["class PRReadiness"]) == {
        "pr_facts.py",
        "pr_readiness.py",
    }
    assert _definition_sites(
        seeded, _PR_READINESS_DEFS["def observe_pr_readiness"]
    ) == {
        "pr_facts.py",
        "pr_readiness.py",
    }
    assert _definition_sites(
        seeded, _PR_READINESS_DEFS["def ci_state_from_conclusion"]
    ) == {
        "pr_facts.py",
        "pr_readiness.py",
    }


# --- Relocated salvage oracle: single home in ``pr_facts.py`` (proposal 106) ---

#: The module proposal 106 retired, as a path relative to ``maestro/``. It must
#: no longer exist after the salvage oracle folded into ``pr_facts.py``.
_RETIRED_PR_SALVAGE_MODULE = "pr_salvage.py"

#: The single module that must define the relocated salvage oracle.
_PR_SALVAGE_HOME = "pr_facts.py"

#: The salvage-oracle definitions and the ``class``/``def`` pattern that finds each
#: one. The trailing ``\b`` keeps a longer name that merely starts with one of these
#: (``SalvageV2`` / ``assess_pr_salvage_v2`` / ``classify_pr_disposability_v2``) from
#: false-matching. Unlike the readiness section, a leading ``\b`` (not the column-0
#: anchor) suffices: the module-level ``assess_pr_salvage`` / ``classify_pr_disposability``
#: names do not collide with any indented method (the agent seam is
#: ``_assess_pr_salvage`` / ``_pr_disposability``), so no indented ``def`` can be
#: mistaken for a second home.
_PR_SALVAGE_DEFS = {
    "class Salvage": re.compile(r"\bclass\s+Salvage\b"),
    "def assess_pr_salvage": re.compile(r"\bdef\s+assess_pr_salvage\b"),
    "def classify_pr_disposability": re.compile(
        r"\bdef\s+classify_pr_disposability\b"
    ),
}


def test_retired_pr_salvage_module_is_absent() -> None:
    """``maestro/pr_salvage.py`` stays deleted after the fold into pr_facts."""
    assert not (_MAESTRO_ROOT / _RETIRED_PR_SALVAGE_MODULE).exists(), (
        "Proposal 106 folded pr_salvage.py into pr_facts.py; the retired module "
        "reappeared. Delete it — the Salvage verdict, its assess_pr_salvage "
        "decision core, and the classify_pr_disposability predicate live in "
        "pr_facts.py."
    )


def test_salvage_oracle_defined_only_in_pr_facts() -> None:
    """``Salvage`` / ``assess_pr_salvage`` / ``classify_pr_disposability`` live only in pr_facts.py."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the single-home assertions pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    for label, pattern in _PR_SALVAGE_DEFS.items():
        sites = _definition_sites(sources, pattern)
        assert sites == {_PR_SALVAGE_HOME}, (
            f"Proposal 106 homes the salvage oracle in {_PR_SALVAGE_HOME}; "
            f"{label!r} is defined in {sorted(sites)} instead of only there. "
            "Collapse the duplicate — the oracle must have exactly one home."
        )


def test_salvage_definition_sites_distinguishes_single_and_multiple_homes() -> None:
    """Negative control: the scan flags a second home and the clean single home.

    Without this, a broken pattern (matching nothing) would make the single-home
    assertion pass vacuously. Hands in-memory sources to the pure scan, so it
    touches no filesystem, and exercises the ``class`` and both ``def`` patterns
    across a match, a drifting second match, and a non-matching (reference-only)
    source.
    """
    salvage_body = (
        'class Salvage(Enum):\n    SALVAGE_AND_LAND = "salvage_and_land"\n\n'
        "def assess_pr_salvage(readiness, disposable):\n"
        "    return Salvage.SALVAGE_AND_LAND\n\n"
        "def classify_pr_disposability(plan_text, siblings):\n    return False\n"
    )
    seeded = {
        "pr_facts.py": salvage_body,
        # A drifting second copy the single-home test must reject.
        "pr_salvage.py": salvage_body,
        # A source mentioning the oracle only by reference must not count as a home.
        "agent.py": (
            "from maestro.pr_facts import Salvage, assess_pr_salvage  # noqa\n"
        ),
    }
    assert _definition_sites(seeded, _PR_SALVAGE_DEFS["class Salvage"]) == {
        "pr_facts.py",
        "pr_salvage.py",
    }
    assert _definition_sites(
        seeded, _PR_SALVAGE_DEFS["def assess_pr_salvage"]
    ) == {
        "pr_facts.py",
        "pr_salvage.py",
    }
    assert _definition_sites(
        seeded, _PR_SALVAGE_DEFS["def classify_pr_disposability"]
    ) == {
        "pr_facts.py",
        "pr_salvage.py",
    }
