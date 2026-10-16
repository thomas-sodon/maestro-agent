"""AST regression: no warning-silencing exception handlers.

Maestro is built around a fail-loud → RCA doctrine (CLAUDE.md →
"Fail loud — no warning-silencing exception handlers"). The
``run_loop`` iteration handler classifies transient SDK failures via
``_TRANSIENT_SDK_CATEGORIES`` and crashes loudly otherwise, feeding
``crash.py::handle_crash`` for root-cause analysis.

A broad/blind ``except`` whose body logs-and-falls-through (or is
empty/``pass``/bare ``continue``/``break``) violates this doctrine: it converts crash signal into
a warning, hides defects in working functionality, and starves the RCA
pathway. This test walks ``maestro/`` and ``maestro_review/`` and fails
when one is found that is neither in the documented allowlist nor in
an acceptable shape (re-raise / typed-failure return / narrow
exception type).

The detector is **structurally selective** — see the docstring on
``_detect_silencer_handlers`` for the precise rule. Specific-exception
handlers (``except KeyError: pass``) are out of scope by design;
catching them structurally would require a body-shape rule with too
many false positives on legitimate narrow-boundary tolerance.

Broad ``contextlib.suppress(Exception)`` / ``suppress(BaseException)``
``with`` blocks ARE in scope: a broad ``suppress`` is
``except Exception: pass`` by construction, so it is flagged on breadth
alone (no body-shape second gate — see ``_is_broad_suppress``). Narrow
``suppress(SpecificError)`` forms are out of scope for the same reason
specific ``except`` handlers are: the contributor chose a narrow
boundary. A broad ``suppress`` whose enclosing function documents
suppression as its contract is exempted via ``_FUNCTION_ALLOWLIST``.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

# Repo-relative paths to the packages under test.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAESTRO_ROOT = _REPO_ROOT / "maestro"
_MAESTRO_REVIEW_ROOT = _REPO_ROOT / "maestro_review"

# Whole-function exemptions: every flagged broad ``except`` inside the
# named function may bypass the rule. Each entry is
# ``(repo_relative_path, function_name)`` — path-qualified to prevent
# basename collisions between ``maestro/`` and ``maestro_review/``
# (e.g. both packages have a ``cli.py``). The allowlist is reserved
# for sites whose documented contract IS log-and-fall-through. The
# intent of this test is to prevent NEW silencer-shape handlers from
# landing — existing legacy sites are documented here so the fence
# stays green while CLAUDE.md → "Fail loud" governs all new code.
# Every entry is necessity-gated: it must exempt at least one
# currently-flagged handler, so narrowing or removing a handler forces
# deleting its entry in the same change (enforced by
# ``test_allowlist_entries_exempt_a_live_handler``).
_FUNCTION_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    # ── Already-documented best-effort sites ──────────────────────
    # BaseException best-effort during shutdown notification — the loop
    # must shut down cleanly even if the operator notification fails.
    ("maestro/agent.py", "shutdown"),
    # (Exception, asyncio.CancelledError) at the Telegram polling stop
    # in ``run_loop``'s finally block — cleanup cannot itself prevent
    # shutdown, but SystemExit/KeyboardInterrupt still propagate (#398).
    ("maestro/loop/iteration.py", "run_loop"),
    # Crash reporting cannot itself crash the startup path.
    ("maestro/crash.py", "handle_crash"),

    # ── Startup-time recovery / migration (best-effort by contract)
    # Hash backfill (proposal 021) runs at startup; failures log and fall through so the loop
    # still boots — verification recomputes from content anyway.
    ("maestro/migration.py", "migrate_backfill_hashes"),
    # Default-limitation seeder runs at startup alongside the startup
    # steps; failures log and fall through so the loop still boots.
    ("maestro/migration.py", "_seed_self_model"),
    # Lifecycle restore on startup is best-effort; corruption of
    # ``lifecycle.json`` falls back to defaults.
    ("maestro/loop/lifecycle.py", "read_lifecycle_progress"),
    # Signal handlers run from arbitrary contexts; suppression keeps
    # shutdown deterministic.
    ("maestro/agent.py", "_on_shutdown_signal"),
    # Config-loading defaults: a corrupt overlay logs and falls back.
    ("maestro/toml_config.py", "_read_toml"),

    # ── Best-effort journal / notify sites ────────────────────────
    # The rate-limit-idle and peak-throttle journal entries follow
    # the same fail-soft contract as ``_notify_best_effort`` — they
    # decorate the operator-visible state transition, never gate it.
    ("maestro/agent.py", "_enter_rate_limit_idle"),
    ("maestro/agent.py", "_exit_rate_limit_idle"),
    ("maestro/loop/lifecycle.py", "maybe_notify_peak_throttle_transition"),
    ("maestro/loop/lifecycle.py", "maybe_notify_maintenance_transition"),
    # Snapshot-version fallback when ``git rev-parse`` is unavailable.
    ("maestro/loop/implementation.py", "_snapshot_version"),
    # ``_phase_check_merged_plans`` batch-snapshot pre-compute (proposal 106):
    # a best-effort optimisation that computes the diagnostic-snapshot +
    # recent-tasks inputs once per merged-plan batch; on failure it logs
    # (warning) and falls back to per-plan computation. Extracted verbatim
    # from ``run_loop`` onto the phase-runner registry, so its
    # log-and-fall-through contract is unchanged — the exemption moves with it.
    ("maestro/agent.py", "_phase_check_merged_plans"),
    # ``_phase_dream`` tiered-memory archival (proposal 106): the best-effort
    # ``Memory.archive`` rotation is operator-managed cold storage — an I/O
    # failure must never crash the loop, so it logs (warning) and falls
    # through. Extracted verbatim from ``run_loop``'s ``dream`` branch onto the
    # phase-runner registry, so its log-and-fall-through contract is unchanged
    # — the exemption moves with it (mirrors ``_phase_check_merged_plans``).
    ("maestro/agent.py", "_phase_dream"),
    # ``task_record.run_post_task_reflection`` post-task journal guard
    # (proposal 106): the best-effort reflection write logs (warning) and falls
    # through so a journal failure never blocks the completion notice.
    # Extracted verbatim from ``run``'s ``# 6.`` tail — first onto its own
    # ``MaestroAgent`` stage method, then into the ``maestro.loop.task_record``
    # sibling with the receiver changed to a leading ``agent`` argument — so
    # its log-and-fall-through contract is unchanged; the exemption moves with
    # it (mirrors ``_phase_check_merged_plans`` / ``_phase_dream``).
    ("maestro/loop/task_record.py", "run_post_task_reflection"),
    # ``task_delivery.resolve_addressed_review_threads`` per-thread
    # best-effort guard (proposal 106 / 091 step 5): resolving one AI-finding
    # thread on GitHub is best-effort exactly like the §4c-ii reply post — a
    # host error logs (warning) and the lap continues to the next thread,
    # leaving it for the next remediation lap. Extracted verbatim from
    # ``run``'s ``# 5-push-ii.`` block into the ``maestro.loop.task_delivery``
    # sibling, so its log-and-fall-through contract is unchanged — the
    # exemption moves with it (mirrors
    # ``task_record.run_post_task_reflection`` /
    # ``_phase_dream``).
    ("maestro/loop/task_delivery.py", "resolve_addressed_review_threads"),
    # ``task_delivery.apply_push_delivery_reconciliation`` best-effort
    # backstop push (proposal 106 / 091 steps 3-4): the fetch-before-retry and
    # both push-attempt swallows log (warning) and fall through, conceding
    # delivery to Tend PR rather than crashing the completion path. Extracted
    # verbatim from ``run``'s ``# 5-push.`` block into the
    # ``maestro.loop.task_delivery`` sibling, so their
    # log-and-fall-through contract is unchanged — the exemption moves with it
    # (mirrors ``task_delivery.resolve_addressed_review_threads`` /
    # ``task_record.run_post_task_reflection``). ``run``'s four teardown
    # guards were lifted onto the extracted ``_teardown_task_branch`` entry
    # above and its entry-time reset probe onto
    # ``task_setup.prepare_task_branch``, so ``run`` itself shields only the
    # ``disconnect_session`` teardown suppress (its own entry below).
    ("maestro/loop/task_delivery.py", "apply_push_delivery_reconciliation"),
    # ``task_delivery.commit_task_artifacts`` commit-path swallow
    # (proposal 106): the ``git`` staging/commit path logs (warning) and falls
    # through, conceding the branch to ``_teardown_task_branch`` rather than
    # crashing the completion path. Extracted verbatim from ``run``'s ``# 5.``
    # block into the ``maestro.loop.task_delivery`` sibling, so its
    # log-and-fall-through contract is unchanged — the exemption moves with it
    # (mirrors ``task_delivery.apply_push_delivery_reconciliation`` /
    # ``task_delivery.resolve_addressed_review_threads``).
    ("maestro/loop/task_delivery.py", "commit_task_artifacts"),
    # ``deliver_review_thread_replies`` per-reply best-effort guard
    # (proposal 106 / 091): posting one recorded terminal reply to a review
    # thread is best-effort — a host error logs (warning) and the lap continues
    # to the next reply, so the undelivered marker is re-recorded and
    # re-delivered next remediation lap (the §4d gate still sees the thread
    # unanswered, forcing a partial that Tend re-routes) rather than crashing
    # the completion path. Extracted verbatim from ``run``'s ``# 4c-ii.`` block
    # into ``maestro/loop/remediation_stamps.py``, so its log-and-fall-through
    # contract is unchanged — the exemption moves with it (mirrors
    # ``task_setup.prepare_task_branch``).
    ("maestro/loop/remediation_stamps.py", "deliver_review_thread_replies"),
    # ``prepare_task_branch`` entry-time reset probe (proposal 106): the
    # working-branch stage's ``has_changes`` dirty-tree check after the
    # best-effort reset is a diagnostic — a probe failure logs (warning) and
    # falls through so branch selection still proceeds rather than crashing task
    # setup. Extracted verbatim from ``run``'s ``# 2.`` block onto its own stage
    # function in the ``maestro.loop.task_setup`` sibling (``maestro/agent.py``
    # is shrink-only under ``tests/test_source_file_size.py``), so its
    # log-and-fall-through contract is unchanged — the exemption moves with it
    # (mirrors ``task_delivery.commit_task_artifacts`` /
    # ``task_delivery.apply_push_delivery_reconciliation``).
    ("maestro/loop/task_setup.py", "prepare_task_branch"),
    # ``_teardown_task_branch`` restores a clean tree on ``main`` from ``run``'s
    # outer ``finally`` (proposal 106): each of its four VCS ops (discard,
    # checkout, delete-branch, dirty-tree probe) logs (warning/error) and falls
    # through so a failure in one still lets the remaining teardown steps run,
    # rather than leaking a dirty task branch into the next run. Extracted
    # verbatim from ``run``'s teardown closure onto its own method, so its
    # log-and-fall-through contract is unchanged — the exemption moves with it
    # (mirrors ``prepare_task_branch`` /
    # ``task_delivery.commit_task_artifacts``). With the
    # closure lifted out, ``_line_to_function`` attributes those four handlers to
    # this method rather than ``run``.
    ("maestro/agent.py", "_teardown_task_branch"),
    # ``teardown_task_run`` is ``run``'s outer ``finally`` block lifted into the
    # ``maestro.loop.lifecycle`` sibling (proposal 106 — ``maestro/agent.py`` is
    # shrink-only under ``tests/test_source_file_size.py``, so the lift lands in a
    # sibling module rather than growing it): it retains the one best-effort site
    # the finally carried — the ``contextlib.suppress(Exception)`` wrapping
    # ``disconnect_session(run_session)`` — the same SDK-teardown contract as
    # ``_disconnect_best_effort`` / ``_phase_implement_approved_plans`` (a teardown
    # failure must not crash the completed task). The four teardown VCS ops live in
    # ``_teardown_task_branch`` above and the entry-time reset probe in
    # ``task_setup.prepare_task_branch``, leaving this single suppress as the only
    # exempt handler (pinned at 1 by the default single carve-out). With the block
    # lifted out ``run`` holds no flagged handler and drops off the allowlist, and
    # ``_line_to_function`` attributes this suppress to ``teardown_task_run``.
    # Surfaced by the broad-``contextlib.suppress`` detector.
    ("maestro/loop/lifecycle.py", "teardown_task_run"),

    # ── Background introspection (best-effort by design) ──────────
    # Distillation / pair-building / resolution recording produce
    # additional context for the contemplation prompt; if the side
    # path fails, the prompt simply lacks the extra context.
    ("maestro/agent.py", "_distill_findings"),
    # Criteria-anchored outcome assessment is best-effort; on failure
    # the step-count-based outcome is returned unchanged.
    ("maestro/loop/implementation.py", "_assess_criteria_outcome"),
    ("maestro/agent.py", "_build_self_directed_pairs"),
    ("maestro/agent.py", "_record_question_resolutions"),
    # Inbound-message processing pipeline: per-message failures log
    # and continue so one bad message doesn't kill the queue.
    ("maestro/agent.py", "_process_messages"),
    # Proposal 131 counterparts of the ask-the-human arm: the best-effort
    # journal write must not roll back the already-persisted plan disposition
    # (re-admission to approved, or abandonment to obsolete), mirroring
    # ``implement_plan``'s ASK_HUMAN journal handler.
    ("maestro/agent.py", "_readmit_blocked_plan"),
    ("maestro/agent.py", "_abandon_blocked_plan"),
    ("maestro/agent.py", "_process_rejected_plans"),
    # Probe + diagnostic helpers — best-effort observability.
    ("maestro/agent.py", "_probe_rate_limit"),
    ("maestro/diagnostics.py", "build_diagnostic_snapshot"),
    # Publication callback ack helpers — Telegram-side ack failures
    # should not roll back the dashboard-side resolution.
    ("maestro/agent.py", "_on_publication_approve"),
    ("maestro/agent.py", "_on_publication_reject"),
    # Cross-surface ask-thomas helper — Telegram outage tolerated.
    ("maestro/agent.py", "ask_thomas"),

    # ── Main-phase inner best-effort sites (contemplate / evolve /
    # impl lineage) — there is NO "outer guard" here: each phase
    # method's top-level ``try`` is a bare ``try/finally`` with zero
    # ``except`` handlers, so nothing catches a phase-level failure at
    # this level. Every entry below exempts *inner* best-effort sites
    # whose contract is genuinely log-and-fall-through — prompt-context
    # side paths and journal writes inside ``contemplate`` (``evolve``'s three
    # went with reflect, proposal 155 step 4),
    # and the plan-lifecycle side paths of the implementation lineage
    # (``_implement_approved_plans`` / ``implement_plan`` /
    # ``_demote_plan_to_failed``). ``_implement_approved_plans`` holds two:
    # the Telegram ``resolve_plan`` warning and the per-plan isolation handler,
    # which re-raises an ``is_environment_fault`` and swallows an
    # already-demoted plan failure so one plan cannot sink the batch (#4039).
    # (``run``'s own teardown VCS ops moved onto the extracted
    # ``_teardown_task_branch`` entry above, following the ``task_setup.prepare_task_branch`` / ``task_delivery.commit_task_artifacts``
    # precedent.) Their breadth is pinned by ``_ENTRY_HANDLER_BASELINE``,
    # so the exempt surface can only shrink — see CLAUDE.md → "Fail loud".
    ("maestro/loop/contemplation.py", "contemplate"),
    ("maestro/loop/implementation.py", "_implement_approved_plans"),
    ("maestro/loop/implementation.py", "implement_plan"),
    ("maestro/loop/implementation.py", "_demote_plan_to_failed"),
    # The pre-flight refusal cluster the implementation lineage delegates to:
    # ``journal_plan_refusal`` logs loudly (``exc_info=True``) and falls through
    # so a failed reflective note never strands an already-decided refusal.
    ("maestro/loop/plan_refusal.py", "journal_plan_refusal"),

    # ── Artifact-review side paths ────────────────────────────────
    # The phase method's outer LLM-call / parse-result swallows are
    # already-documented best-effort: the reviewer skips a cycle
    # rather than crashing the loop on a single LLM hiccup.
    ("maestro/artifact_review.py", "review"),
    # ``_emit_publication_if_eligible``'s remaining broad excepts
    # cover ``resolve_owner`` and ``repo_exists`` — host-API
    # tolerated failures (auth lapse / network blip) that demote to
    # ``improve`` without blocking.
    ("maestro/artifact_review.py", "_emit_publication_if_eligible"),
    # Backfill is by definition best-effort retry of missed work.
    ("maestro/artifact_review.py", "_maybe_backfill_proposals"),

    # ── CLI entry points — maestro/ (operator-facing best-effort) ─
    ("maestro/cli.py", "cmd_preflight"),
    ("maestro/cli.py", "cmd_notify_pre_deploy"),
    ("maestro/cli.py", "_resolve_pre_deploy_prs"),

    # ── Other best-effort sites ───────────────────────────────────
    # Contribution scanning is observational; failure logs and
    # continues.
    ("maestro/contributions.py", "check_for_contributions"),
    # Diagnostics response builder is best-effort observability polled on
    # every dashboard refresh; an unexpected raise while reading
    # operational state must degrade to a safe fallback, not 500 the
    # panel. The failure is logged loud (``warning`` + ``exc_info``) for
    # RCA while the response keeps a stable shape — mirrors
    # loop/contemplation.py::contemplate. Extracted to a module-level helper so this
    # allowlist entry exempts only this guard, not the whole create_router
    # factory (nested closures resolve to their outer function).
    ("maestro/dashboard/routes.py", "_diagnostics_response"),
    # Phase-cost report refresh is zero-SDK-cost file I/O run by
    # the dream phase each pass; failure must never crash the dream.
    ("maestro/phase_cost_report.py", "refresh_phase_cost_report"),
    # Evolution side paths — submit_for_review, _reconcile_orphan_branches,
    # check_merged_plans — all log and continue so the rest of the phase
    # runs.  learn_from_rejection narrowed its swallow to transient SDK
    # failure (#1404), so it no longer needs an entry here.
    # The two merge-state readers absorb a *fail-loud* dependency knowingly
    # (#2958): both observe through the ``is_pr_settled`` chokepoint, whose
    # ``git fetch`` re-raises so no verdict is authored on unfreshened refs.
    # Absorbing preserves exactly that property — the swallowed arm authors
    # nothing (no status flip, no repair credit), leaving the plan
    # ``implemented`` for the next iteration — while keeping the failure
    # per-plan, so one unreachable PR cannot strand every other plan's flip.
    # Deferring beats crashing here because both callers are cadence phases
    # whose only output is a status flip that is correct one iteration later;
    # and the crash signal is not lost, since the faces that *act* on merge
    # truth (the remediation arms in ``agent.py`` / ``loop/pr_remediation.py``,
    # the settlement reconciler) call ``is_pr_settled`` unwrapped.
    ("maestro/evolution_submission.py", "submit_for_review"),
    ("maestro/evolution_tend.py", "_reconcile_orphan_branches"),
    ("maestro/evolution.py", "check_merged_plans"),
    # Slice promotion (proposal 118) reuses submit_for_review's best-effort
    # review-notification tail verbatim: the status flip + journal note have
    # already landed, so a Telegram/notify outage must not undo the promotion
    # or crash the loop phase.
    ("maestro/evolution.py", "promote_slice"),
    # Refined-plan notification is best-effort, mirroring submit_for_review:
    # the curate phase has already refined the plan and journalled it; a
    # Telegram/notify outage must not undo that or crash the loop phase.
    ("maestro/evolution.py", "_apply_curate_refine"),
    # Executor cleanup / fallback paths — failure to end a session
    # or to fall back must not crash the executor.
    ("maestro/executor.py", "execute"),
    ("maestro/executor.py", "end_session"),
    # GitHub issue close/comment is fire-and-forget.
    ("maestro/issues.py", "close"),
    ("maestro/issues.py", "comment"),
    # Telegram message-send / message-edit retries are tolerated by
    # the polling-loop contract.
    ("maestro/notifications/telegram.py", "_claim_polling_session"),
    ("maestro/notifications/telegram.py", "_handle_callback"),
    ("maestro/notifications/telegram.py", "resolve_plan"),
    ("maestro/notifications/telegram.py", "_retire_plan_message"),
    ("maestro/notifications/telegram.py", "resolve_publication"),
    ("maestro/notifications/telegram.py", "_update_message_after_action"),
    ("maestro/notifications/telegram.py", "_update_message_already_resolved"),
    # Publication notify helpers — Telegram outage tolerated.
    ("maestro/publication.py", "_notify_success"),
    ("maestro/publication.py", "_notify_failure"),
    # Cross-surface Telegram-button cleanup — UI hygiene, not a
    # publication step. A bot-API outage must not break the publish
    # pipeline (mirror of agent.py's resolve_plan wrappers).
    ("maestro/publication.py", "_publish"),
    ("maestro/publication.py", "_resolve_rejected_publications"),
    # ``discard_changes`` cleanup must never raise.
    ("maestro/vcs.py", "discard_changes"),

    # ── SDK session-teardown contract (broad ``suppress``) ────────
    # ``_disconnect_session_impl`` is finally-safe by construction: its own
    # docstring records that every call site wraps the bare ``disconnect()`` in
    # ``contextlib.suppress(Exception)`` and that the substitution must preserve
    # that contract. Suppression IS the documented contract, so the broad
    # ``suppress`` inside it is allowlisted rather than narrowed.
    ("maestro/sdk.py", "_disconnect_session_impl"),
    # ``_disconnect_best_effort`` is the three-line wrapper that lifts the two
    # identical ``suppress(Exception): await _disconnect_session_impl(client)``
    # blocks out of ``_create_session_impl``'s cancel-recovery and
    # resume-fallback arms (extracted so the hundreds-of-lines
    # ``_create_session_impl`` is NOT itself allowlisted, which would exempt
    # every future broad silencer inside it). Both arms re-raise / retry the
    # primary failure right after teardown, so a secondary disconnect error
    # must never displace it.
    ("maestro/sdk.py", "_disconnect_best_effort"),
    # The handlers below re-raise conditionally, so they are exempt only via
    # this list, not via the body shape (#4165 — the fence counts a handler as
    # a re-raise only when *every* path ends in ``raise``). Each is pinned at
    # the default single handler, so a second swallow cannot ride the entry.
    # ``_query_impl`` / ``_query_on_session_impl``'s ``except BaseException``
    # internal-cancel containment re-raises every outside-party cancel; only an
    # internal anyio deadline leak falls through, warned and converted to an
    # exception that the retry/reclassify tail re-raises unless it is
    # transient and within the retry budget.
    ("maestro/sdk.py", "_query_impl"),
    ("maestro/sdk.py", "_query_on_session_impl"),
    # ``_phase_implement_approved_plans`` ends Session A before per-plan
    # implementation via ``suppress(Exception): await disconnect_session(...)``
    # — the same best-effort SDK-teardown contract as ``_disconnect_best_effort``
    # (the three already-allowlisted ``agent.py`` phase methods —
    # ``teardown_task_run``, ``implement_plan``, ``run_loop`` — wrap the same
    # ``disconnect_session`` call). A teardown failure must not crash the phase.
    ("maestro/agent.py", "_phase_implement_approved_plans"),

    # ── maestro_review/ ───────────────────────────────────────────
    # CLI entry point is best-effort by contract; a single bad
    # invocation should not crash the whole batch.
    ("maestro_review/cli.py", "main"),
    ("maestro_review/cli.py", "_load_review_defaults"),
    # The disputed-finding issue is already created when the thread-annotation
    # reply is attempted; a transient reply failure logs and continues so the
    # remaining findings in the pass are not dropped.
    ("maestro_review/review.py", "_handle_disagreements"),
    # ``_patch_sdk_stderr_drain``'s ``suppress(TimeoutError, Exception)`` guards
    # the best-effort stderr drain before the primary ``ProcessError`` re-raise
    # on the very next line. ``TaskHandle.wait()`` re-raises the task's own
    # exception, so the broad arm keeps a secondary stderr-reader failure from
    # displacing the primary error signal — narrowing would trade the primary
    # error for a secondary one, so it is allowlisted, not narrowed.
    ("maestro_review/sdk.py", "_patch_sdk_stderr_drain"),
    # ``query``'s retry handler logs captured CLI stderr at ``error`` and falls
    # through only to retry a transient failure; a non-transient failure or the
    # final attempt re-raises, so no failure is swallowed (#4165).
    ("maestro_review/sdk.py", "query"),
})


# The two exception names that make an ``except`` handler or a
# ``contextlib.suppress`` broad. Hoisted to a module constant so the breadth
# definition cannot drift between the handler arm (``_is_broad_handler``) and
# the ``suppress`` arm (``_is_broad_suppress``).
_BROAD_EXC_NAMES = frozenset({"Exception", "BaseException"})

# A flagged node is either a silencer-shape ``except`` handler or a broad
# ``contextlib.suppress`` ``with`` block — see ``_detect_silencer_handlers``.
_FlaggedNode = ast.ExceptHandler | ast.With | ast.AsyncWith


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    """Return ``True`` when *handler* is bare, ``Exception``,
    ``BaseException``, or a tuple containing either of those.

    Anything else (specific name, attribute access like
    ``asyncio.CancelledError``, tuple of only-specific types) is
    intentionally not flagged — the contributor chose a narrow
    boundary, which is the recommended form.
    """
    typ = handler.type
    if typ is None:
        return True
    if isinstance(typ, ast.Name) and typ.id in _BROAD_EXC_NAMES:
        return True
    if isinstance(typ, ast.Tuple):
        return any(
            isinstance(elt, ast.Name) and elt.id in _BROAD_EXC_NAMES
            for elt in typ.elts
        )
    return False


def _is_broad_suppress_item(context_expr: ast.expr) -> bool:
    """Return ``True`` when a single ``with`` item is a broad ``suppress(...)``.

    True for ``contextlib.suppress(...)`` (``ast.Attribute`` ``suppress``) or a
    bare from-imported ``suppress(...)`` (``ast.Name`` ``suppress``) carrying a
    positional ``ast.Name`` arg in :data:`_BROAD_EXC_NAMES`. Narrow forms
    (``suppress(VCSError)``, ``suppress(ValueError, TypeError)``) name the
    boundary the contributor chose and are intentionally not flagged, mirroring
    ``_is_broad_handler``.
    """
    if not isinstance(context_expr, ast.Call):
        return False
    func = context_expr.func
    is_suppress = (
        (isinstance(func, ast.Attribute) and func.attr == "suppress")
        or (isinstance(func, ast.Name) and func.id == "suppress")
    )
    if not is_suppress:
        return False
    return any(
        isinstance(arg, ast.Name) and arg.id in _BROAD_EXC_NAMES
        for arg in context_expr.args
    )


def _is_broad_suppress(node: ast.With | ast.AsyncWith) -> bool:
    """Return ``True`` when any ``with`` item is a broad ``contextlib.suppress``.

    A broad ``contextlib.suppress(Exception)`` is ``except Exception: pass`` by
    construction — the named types are discarded and control resumes after the
    block. There is no re-raise shape available inside it and logging in the
    body is irrelevant, so (unlike an ``except`` handler) the ``suppress`` arm
    is gated on breadth alone, with no ``_body_is_silencer`` second gate.
    """
    return any(_is_broad_suppress_item(item.context_expr) for item in node.items)


def _always_raises(stmts: list[ast.stmt]) -> bool:
    """Return ``True`` iff every path through *stmts* leaves via ``raise``.

    Deliberately conservative: the block must end in an ``ast.Raise``, or in
    an ``ast.If`` whose ``body`` and (non-empty) ``orelse`` both always raise,
    or in a ``with`` whose body always raises. A ``raise`` guarded by an
    ``if`` with no ``else``, or nested inside a loop / inner ``try``, may fall
    through — so a handler shaped ``if rare(exc): raise`` then
    ``logger.exception(...)`` is still a log-and-fall-through swallow (#4165).
    """
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.If):
        return _always_raises(last.body) and _always_raises(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _always_raises(last.body)
    return False


def _body_is_silencer(handler: ast.ExceptHandler) -> bool:
    """Return ``True`` when the handler body silences the exception.

    Structural rule:

    - If every path through the body ends in ``raise`` (``_always_raises``),
      it is a re-raise / log-and-rethrow shape — not a silencer. A
      conditional ``raise`` that other exceptions fall past does not count.
    - Otherwise, a silencer is one of:
      * **log-and-fall-through** — the body contains a call whose
        ``func`` is ``logger.{warning,exception,error,critical}``,
        ``log.{warning,exception,error,critical}``, or a bare
        ``print(...)``.
      * **empty / pass / bare loop control** — every top-level statement
        is ``ast.Pass``, ``ast.Continue``, ``ast.Break``, or a docstring
        (``ast.Expr`` of an ``ast.Constant``). A ``continue``/``break``-only
        body logs nothing and returns nothing, so it is as silent as
        ``pass`` (#4129).

    Anything else (typed-failure return, structural state mutation that
    surfaces failure to the caller) is not a silencer — the caller
    sees the failure.

    ``debug``/``info`` are **excluded by design**, mirroring the
    specific-exception exclusion (module docstring): production
    debug/info logging is a legitimate, widely-used best-effort
    pattern — broad handlers across ``maestro/sdk.py``,
    ``maestro/email.py``, and ``maestro/agent.py`` deliberately
    ``logger.debug(..., exc_info=True)`` a swallowed failure. Such
    debug-level swallows are out of the detector's scope and rely on
    human review rather than this AST fence; broadening ``log_attrs``
    to include them would reverse that pattern across the tree. The
    ``test_synthetic_broad_except_with_debug_log_is_not_flagged`` pin
    keeps the boundary intentional — adding ``debug``/``info`` here
    fails it, making any future broadening a conscious, reviewed change.
    The excluded sites are themselves enumerated per file and shrink-only
    via ``_DEBUG_SWALLOW_BASELINE``, so the carve-out cannot grow silently.

    The **functional logging form** ``logger.log(level, ...)`` is
    likewise not detected — only the attribute forms above
    (``logger.warning`` … ``logger.critical``) match ``log_attrs``.
    This is the same boundary as the debug/info exclusion for the same
    reason: the level is a runtime argument the AST cannot classify, so
    ``.log`` cannot join ``log_attrs`` without also flagging
    ``logger.log(logging.DEBUG, ...)`` swallows and reversing that
    exclusion. A broad handler whose only silencing call is
    ``logger.log(...)`` (e.g. ``agent.py::_notify_best_effort``'s
    ``logger.log(log_level, ...)`` contract) therefore relies on human
    review, not this fence — widening ``log_attrs`` to reach it is a
    separate, deliberate decision.
    """
    if _always_raises(handler.body):
        return False

    log_attrs = {"warning", "exception", "error", "critical"}
    log_names = {"logger", "log"}
    for sub in ast.walk(handler):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in log_attrs
            and isinstance(func.value, ast.Name)
            and func.value.id in log_names
        ):
            return True
        if isinstance(func, ast.Name) and func.id == "print":
            return True

    return all(
        isinstance(stmt, (ast.Pass, ast.Continue, ast.Break))
        or (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
        )
        for stmt in handler.body
    )


def _detect_silencer_handlers(
    tree: ast.AST,
) -> list[tuple[int, _FlaggedNode]]:
    """Return ``(lineno, node)`` for every silencer-shape node in *tree*.

    Two node shapes are flagged:

    - An ``except`` handler, iff both:

      1. ``_is_broad_handler`` — bare, ``Exception``, ``BaseException``,
         or a tuple containing either.
      2. ``_body_is_silencer`` — log-and-fall-through, empty/pass/bare
         loop control, where not every path of the body ends in ``raise``.

    - A ``with`` / ``async with`` block, iff ``_is_broad_suppress`` — it holds
      a broad ``contextlib.suppress(Exception)`` item. This arm is gated on
      breadth **alone**: a broad ``suppress`` is ``except Exception: pass`` by
      construction, so there is no body-shape second gate (see
      ``_is_broad_suppress``). A site that re-raises the *primary* exception
      right after the block is still flagged by breadth alone, so each such
      site needs an explicit narrow / allowlist disposition.
    """
    flagged: list[tuple[int, _FlaggedNode]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if _is_broad_handler(node) and _body_is_silencer(node):
                flagged.append((node.lineno, node))
        elif isinstance(node, (ast.With, ast.AsyncWith)) and _is_broad_suppress(
            node,
        ):
            flagged.append((node.lineno, node))
    return flagged


def _line_to_function(tree: ast.AST) -> dict[int, str]:
    """Map ``lineno`` → enclosing top-level function name within *tree*.

    Inner closures resolve to their outer named function (``setdefault``
    keeps the first-seen mapping, which is the outer one because we
    walk in source order).
    """
    line_to_func: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            line = getattr(child, "lineno", None)
            if line is not None:
                line_to_func.setdefault(line, node.name)
    return line_to_func


def _describe(node: _FlaggedNode) -> str:
    """Short human description of the flagged node's clause.

    For a broad ``contextlib.suppress`` ``with`` block, renders the offending
    ``suppress(...)`` call via ``ast.unparse`` (e.g.
    ``with contextlib.suppress(Exception)``) so the violator line stays legible.
    """
    if isinstance(node, (ast.With, ast.AsyncWith)):
        offending = [
            ast.unparse(item.context_expr)
            for item in node.items
            if _is_broad_suppress_item(item.context_expr)
        ]
        return f"with {offending[0]}"
    typ = node.type
    if typ is None:
        return "bare except"
    if isinstance(typ, ast.Name):
        return f"except {typ.id}"
    if isinstance(typ, ast.Tuple):
        names = []
        for elt in typ.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.append(ast.unparse(elt))
            else:
                names.append("?")
        return f"except ({', '.join(names)})"
    return f"except {ast.unparse(typ)}"


@functools.cache
def _scan_flagged_sites() -> tuple[tuple[str, int, str, _FlaggedNode], ...]:
    """Return every flagged node in the scanned tree, once per run.

    Returns a tuple of ``(repo_relative_path, lineno, enclosing_function,
    node)`` for each silencer-shape node ``_detect_silencer_handlers``
    finds under ``maestro/`` and ``maestro_review/`` — broad ``except`` handlers
    and broad ``contextlib.suppress`` ``with`` blocks alike. Memoized because
    the tree is immutable for the duration of a run and both the fence and the
    allowlist hygiene check consume it — a second independent walk would double
    a scan that already dominates this module's runtime under coverage.
    """
    sites: list[tuple[str, int, str, _FlaggedNode]] = []
    for root in (_MAESTRO_ROOT, _MAESTRO_REVIEW_ROOT):
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(_REPO_ROOT))
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            line_to_func = _line_to_function(tree)
            for lineno, handler in _detect_silencer_handlers(tree):
                func_name = line_to_func.get(lineno, "<module>")
                sites.append((rel, lineno, func_name, handler))
    return tuple(sites)


def _stale_allowlist_entries(
    allowlist: frozenset[tuple[str, str]],
    covered: set[tuple[str, str]],
) -> list[str]:
    """Return ``path::func`` for each allowlist entry exempting no flagged handler.

    *covered* is the set of ``(path, function)`` pairs that own at least one
    currently-flagged handler. An entry outside it is dead: it exempts nothing
    today, so it is a standing blind spot rather than a documented carve-out.
    """
    return [
        f"{rel}::{func_name}"
        for rel, func_name in sorted(allowlist - covered)
    ]


# Per-entry breadth pins. The allowlist is a whole-function exemption
# (``test_allowlist_entries_exempt_a_live_handler`` only proves each entry
# shields *at least one* handler), so nothing bounds how many silencers a
# single entry may accumulate — a new silencer-shape handler added inside an
# already-listed function lands unflagged. This dict pins the exempt-handler
# count of every entry that legitimately shields more than one handler; an
# entry absent from it is pinned at exactly ``1`` (the default single carve-out).
# ``test_allowlist_entry_breadth_is_ratcheted`` fails when the live count of any
# entry diverges from its pin, so allowlist breadth can only move by a deliberate,
# reviewed baseline edit — the exempt surface only ever shrinks.
# The ``run_loop`` / ``implement_plan`` pins each carry one extra handler over
# the whole-function-extraction baseline: the ``contextlib.suppress(Exception)``
# around their ``disconnect_session`` call, surfaced by the
# broad-``contextlib.suppress`` detector and allowlisted as best-effort SDK
# teardown (the same contract as ``teardown_task_run`` / ``_disconnect_best_effort``).
# ``implement_plan`` carries one further handler beyond that: the best-effort
# journal write in the proposal-131 ASK_HUMAN disposition arm, which logs loudly
# (``exc_info=True``) and falls through so a failed reflective note never strands
# an already-``blocked`` plan whose human question was already sent. (Its
# scope-gate journal write left for ``loop/plan_refusal.py``'s shared
# ``journal_plan_refusal``, lowering this pin from 5 to 4; the inherited-hold
# arm's self-model bookkeeping then moved to
# ``loop/plan_holds.py::record_inherited_hold``, where its single broad handler
# split into three narrow ``except (OSError, ValueError)`` guards — self-model
# load, journal note, and base-repair directive save, each naming what was lost
# — lowering this pin again from 4 to 3, since a narrow handler is not exempted
# machinery at all.)
# ``maestro/loop/task_setup.py``'s ``prepare_task_branch`` (pinned at 2) shields
# no ``disconnect_session`` call;
# its two handlers are the entry-time ``contextlib.suppress(Exception)`` around
# ``discard_changes()`` and the broad ``except`` around the ``has_changes()``
# dirty-tree probe — both best-effort entry-time working-tree resets that log
# and fall through.
_ENTRY_HANDLER_BASELINE: dict[tuple[str, str], int] = {
    ("maestro/loop/iteration.py", "run_loop"): 11,
    ("maestro/loop/contemplation.py", "contemplate"): 7,
    ("maestro/cli.py", "cmd_notify_pre_deploy"): 5,
    ("maestro/agent.py", "_teardown_task_branch"): 4,
    ("maestro/artifact_review.py", "_maybe_backfill_proposals"): 4,
    ("maestro/cli.py", "cmd_preflight"): 4,
    ("maestro_review/cli.py", "main"): 4,
    ("maestro/loop/task_delivery.py", "apply_push_delivery_reconciliation"): 3,
    ("maestro/loop/implementation.py", "implement_plan"): 3,
    ("maestro/agent.py", "shutdown"): 3,
    ("maestro/notifications/telegram.py", "_handle_callback"): 3,
    ("maestro/loop/implementation.py", "_implement_approved_plans"): 2,
    ("maestro/loop/task_setup.py", "prepare_task_branch"): 2,
    ("maestro/agent.py", "_on_shutdown_signal"): 2,
    ("maestro/loop/implementation.py", "_snapshot_version"): 2,
    ("maestro/artifact_review.py", "_emit_publication_if_eligible"): 2,
    ("maestro/artifact_review.py", "review"): 2,
    ("maestro/contributions.py", "check_for_contributions"): 2,
    ("maestro/crash.py", "handle_crash"): 2,
    ("maestro/evolution_tend.py", "_reconcile_orphan_branches"): 2,
    ("maestro/loop/lifecycle.py", "maybe_notify_maintenance_transition"): 2,
    ("maestro/loop/lifecycle.py", "maybe_notify_peak_throttle_transition"): 2,
    ("maestro/publication.py", "_notify_success"): 2,
}


def _overbroad_allowlist_entries(
    allowlist: frozenset[tuple[str, str]],
    counts: dict[tuple[str, str], int],
    baseline: dict[tuple[str, str], int],
) -> list[str]:
    """Return ``path::func expected N, found M`` for each mis-pinned entry.

    *counts* maps ``(path, function)`` → number of currently-flagged handlers
    that entry exempts. *baseline* pins the expected count for every entry that
    legitimately shields more than one handler; an entry absent from *baseline*
    is pinned at exactly ``1``. An entry whose live count is **above** its pin
    has grown a silencer nothing reviewed; one **below** its pin was narrowed
    and must lower its pin in the same change. Both directions are reported so
    breadth moves only by a deliberate baseline edit — never silently.
    """
    overbroad: list[str] = []
    for entry in allowlist:
        pinned = baseline.get(entry, 1)
        found = counts.get(entry, 0)
        if found != pinned:
            rel, func_name = entry
            overbroad.append(
                f"{rel}::{func_name} expected {pinned}, found {found}",
            )
    return sorted(overbroad)


def _dead_baseline_pins(
    baseline: dict[tuple[str, str], int],
    allowlist: frozenset[tuple[str, str]],
) -> list[str]:
    """Return ``path::func`` for each baseline pin whose entry left the allowlist.

    ``_overbroad_allowlist_entries`` iterates the *allowlist*, so a pin in
    *baseline* whose ``(path, function)`` is no longer in *allowlist* bounds
    nothing — it is dead config that rots silently and, worse, would silently
    re-apply its stale count if that entry were ever re-added. This is the
    symmetric blind spot ``_stale_allowlist_entries`` closes for the allowlist
    itself; reporting it forces a dropped allowlist entry to drop its pin in the
    same change.
    """
    return [
        f"{rel}::{func_name}"
        for rel, func_name in sorted(set(baseline) - allowlist)
    ]


# ---------------------------------------------------------------------------
# Documented-exemption anchors.
#
# The allowlist's contract is that each entry's log-and-fall-through is a
# *reviewed* fail-soft boundary, but that review left no durable trace outside
# the allowlist comment itself — a reader of the architecture surface cannot
# tell a blessed boundary from an accidentally widened silencer. An entry may
# opt in here by naming a ``docs/`` section stating its contract; the anchor is
# then machine-checked, so the doc cannot be deleted or its heading renamed out
# from under the exemption. Opt-in by design: an unmapped entry is not a
# failure, a mapped-but-unresolvable one is.
_ENTRY_DOC_ANCHOR: dict[tuple[str, str], tuple[str, str]] = {
    ("maestro/dashboard/routes.py", "_diagnostics_response"): (
        "docs/dashboard-security.md",
        "## Diagnostics Fail-Soft Boundary",
    ),
}


def test_documented_exemptions_resolve_to_a_live_anchor() -> None:
    """Every ``_ENTRY_DOC_ANCHOR`` entry names a live allowlist entry and doc.

    Two failure modes, both silent without this gate: the doc section an
    exemption cites is deleted or renamed (the rationale evaporates while the
    exemption stays), and the allowlist entry is dropped while its anchor rots
    behind — the same dead-config blind spot ``_dead_baseline_pins`` closes for
    ``_ENTRY_HANDLER_BASELINE``.
    """
    dead = _dead_baseline_pins(
        dict.fromkeys(_ENTRY_DOC_ANCHOR, 0), _FUNCTION_ALLOWLIST,
    )
    assert dead == [], (
        "_ENTRY_DOC_ANCHOR entries whose (path, function) is no longer in "
        "_FUNCTION_ALLOWLIST — dead config anchoring nothing:\n  "
        + "\n  ".join(dead)
        + "\n\nRemove each dead anchor (its allowlist entry was dropped), or "
        "restore the allowlist entry if it went missing by mistake."
    )

    unresolved: list[str] = []
    for (rel, func_name), (doc_rel, heading) in sorted(
        _ENTRY_DOC_ANCHOR.items(),
    ):
        doc_path = _REPO_ROOT / doc_rel
        if not doc_path.is_file():
            unresolved.append(f"{rel}::{func_name} -> missing doc {doc_rel}")
            continue
        lines = doc_path.read_text(encoding="utf-8").splitlines()
        if heading not in lines:
            unresolved.append(
                f"{rel}::{func_name} -> {doc_rel} has no heading {heading!r}",
            )

    assert unresolved == [], (
        "_ENTRY_DOC_ANCHOR entries whose documented rationale no longer "
        "resolves:\n  "
        + "\n  ".join(unresolved)
        + "\n\nRestore the doc section (or update the anchor to its new "
        "heading) so the exemption keeps its durable justification.\n\nSee "
        "CLAUDE.md -> 'Fail loud - no warning-silencing exception handlers'."
    )


# ---------------------------------------------------------------------------
# Debug/info swallow carve-out ratchet.
#
# ``_body_is_silencer`` matches only ``logger.{warning,exception,error,
# critical}``, so a broad handler whose sole logging call is
# ``logger.{debug,info}`` is never flagged — a deliberate carve-out resting on
# human review (see that function's docstring). Nothing bounded that carve-out's
# size, so each new debug/info swallow landed green and invisible, exactly the
# silent accretion ``_ENTRY_HANDLER_BASELINE`` prevents for the warning-and-above
# class. ``_DEBUG_SWALLOW_BASELINE`` pins the carve-out's per-file site count so
# the next one is a conscious, reviewed diff.
#
# The allowlist deliberately plays no part here: it exempts *flagged* handlers,
# and these sites are never flagged, so filtering by it would hide a subset of
# the carve-out from its own pin.
_DEBUG_SWALLOW_LEVELS = frozenset({"debug", "info"})
_LOUD_LOG_LEVELS = frozenset({"warning", "exception", "error", "critical"})
_LOGGER_NAMES = frozenset({"logger", "log"})


def _body_is_debug_swallow(handler: ast.ExceptHandler) -> bool:
    """Return ``True`` when *handler* swallows a failure at debug/info level only.

    The shape ``_body_is_silencer`` carves out: not every path of the body
    ends in ``raise`` (``_always_raises``), at least one
    ``logger.{debug,info}`` call, and **no** call the fence itself treats as a
    silencer — neither ``logger.{warning,exception,error,critical}`` nor a bare ``print(...)``. Excluding *every* silencer shape, not
    just the loud log levels, is what keeps the two pins disjoint: a handler
    that pairs ``logger.debug`` with a ``print`` is already flagged by the
    fence, so counting it here would pin the same site twice and let a genuine
    fence violation masquerade as carve-out growth. Breadth
    (``_is_broad_handler``) is the caller's gate, mirroring
    ``_detect_silencer_handlers``.
    """
    if _always_raises(handler.body):
        return False

    levels: set[str] = set()
    for sub in ast.walk(handler):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name) and func.id == "print":
            return False
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _LOGGER_NAMES
        ):
            levels.add(func.attr)
    if levels & _LOUD_LOG_LEVELS:
        return False
    return bool(levels & _DEBUG_SWALLOW_LEVELS)


@functools.cache
def _scan_debug_swallow_sites() -> tuple[tuple[str, int, str], ...]:
    """Return ``(repo_relative_path, lineno, enclosing_function)`` per carve-out site.

    Walks ``maestro/`` **and** ``maestro_review/`` — exactly the roots
    ``_scan_flagged_sites`` covers — flagging every broad handler
    ``_body_is_debug_swallow`` matches. Scanning the narrower tree would leave
    ``maestro_review/`` free to accrete the very swallows this pin exists to
    bound; it holds none today, so covering it costs nothing but closes that
    path. Memoized for the same reason as the fence's scan: the tree is
    immutable for a run.
    """
    sites: list[tuple[str, int, str]] = []
    for root in (_MAESTRO_ROOT, _MAESTRO_REVIEW_ROOT):
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(_REPO_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            line_to_func = _line_to_function(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if _is_broad_handler(node) and _body_is_debug_swallow(node):
                    sites.append(
                        (rel, node.lineno, line_to_func.get(node.lineno, "<module>")),
                    )
    return tuple(sorted(sites))


# Per-file pins for the debug/info carve-out, regenerated from the tree. Like
# ``_ENTRY_HANDLER_BASELINE`` this is shrink-only in spirit: a file whose count
# rises has grown an unreviewed swallow, one whose count falls must lower its
# pin (or drop the key) in the same change.
_DEBUG_SWALLOW_BASELINE: dict[str, int] = {
    # 7 -> 5 (#3803): the two rejection-phase disconnect swallows moved out to
    # ``maestro/loop/rejection_sessions.py`` with the session pair itself.
    # 5 -> 4 (proposal 149): the doc-proposals session disconnect swallow
    # left with the retired ``propose_task_docs`` phase.
    "maestro/agent.py": 4,
    "maestro/email.py": 3,
    "maestro/loop/lifecycle.py": 1,
    # Proposal 133: ``hold_plans_on_environment``'s activation-row and journal-note
    # writers. Both are records *about* a hold already stamped on the plans; a
    # ledger or journal that cannot be written must not cost the hold itself,
    # which is the thing protecting the attempt budget.
    # Bumped 2 -> 3 for proposal 133 step 5: ``stamp_terminal_environment_hold``
    # is the terminal twin of ``hold_plans_on_environment`` and carries the same
    # best-effort ``gate_environment_hold`` ledger row for the same reason — the
    # row makes the hold visible to ``maestro audit-flags``, but the *stamp* is
    # what protects the attempt budget, so a ledger that cannot be written must
    # not cost the hold. Written after the stamp, so the hold survives either way.
    "maestro/loop/plan_holds.py": 3,
    "maestro/loop/pr_remediation.py": 1,
    # #3803: the rejection session pair's best-effort teardown. The phase's
    # result is already recorded by the time the sessions close, so a
    # disconnect failure must not mask it — and the second session must still
    # be torn down when the first one's disconnect raises.
    "maestro/loop/rejection_sessions.py": 1,
    # #4129: +1 for ``_query_on_session_impl``'s post-interrupt drain. The
    # caller already holds its result; the drain is bounded defensive cleanup,
    # so an unexpected iterator failure just ends it (logged at debug).
    "maestro/sdk.py": 9,
    "maestro/task_gates.py": 1,
}


# Full-AST scan of every maestro/ + maestro_review/ source file; mocking the
# dependency is impossible — walking the real AST is the test's purpose. As
# agent.py grew, this drifted toward the 5s unit cap under coverage; raise to
# the component budget so it stays green deterministically under suite load.
@pytest.mark.timeout(15)
def test_no_warning_silencing_exception_handlers() -> None:
    """Fail when a defect-silencing broad ``except`` lands.

    See module docstring for the doctrine and CLAUDE.md → "Fail loud".
    """
    violators: list[str] = []
    for rel, lineno, func_name, handler in _scan_flagged_sites():
        if (rel, func_name) in _FUNCTION_ALLOWLIST:
            continue
        violators.append(
            f"{rel}:{lineno} (in {func_name}): {_describe(handler)}",
        )

    assert not violators, (
        "Defect-silencing broad handlers (a broad/blind `except` whose body "
        "logs-and-falls-through with no `raise`, or a broad "
        "`contextlib.suppress(Exception)`):\n  "
        + "\n  ".join(violators)
        + "\n\nEach `except` handler must either:\n"
        + "  1. Re-raise unconditionally (end every path of the body with "
        + "`raise` — log-and-rethrow is fine; a `raise` guarded by an `if` "
        + "still falls through)\n"
        + "  2. Narrow the exception type to specific subclasses (the "
        + "detector stops flagging once the handler is no longer broad)\n"
        + "  3. Replace the log-and-fall-through with a typed-failure "
        + "return that surfaces the failure to the caller (no logger call)\n"
        + "  4. Be added to _FUNCTION_ALLOWLIST with documented "
        + "justification — reserved for sites whose contract IS "
        + "log-and-fall-through\n"
        + "A broad `contextlib.suppress` is a silencer by construction (no "
        + "body-shape escape), so only remedies 2 and 4 apply: narrow the "
        + "suppressed types (e.g. `suppress(VCSError)`) or, when suppression "
        + "IS the documented contract, add the enclosing function to "
        + "_FUNCTION_ALLOWLIST.\n"
        + "\nSee CLAUDE.md → 'Fail loud — no warning-silencing exception "
        + "handlers'."
    )


# Consumes the same memoized whole-tree scan as the fence above; the walk is
# paid once per run, but keep the component budget so a cold cache (this test
# running first) stays green under suite load.
@pytest.mark.timeout(15)
def test_allowlist_entries_exempt_a_live_handler() -> None:
    """Every allowlist entry must exempt at least one currently-flagged handler.

    The allowlist is a **whole-function** exemption: once ``(path, function)``
    is listed, any broad silencer added anywhere inside that function passes
    silently. An entry whose handlers were since narrowed, moved, or deleted
    therefore exempts nothing — it is a permanent blind spot exactly where
    CLAUDE.md → "Fail loud" points the fence, and nothing else measures it.

    Keeping this assertion green is self-extinguishing: narrowing a handler
    forces removing its entry in the same change, so the exempt surface only
    ever shrinks.
    """
    covered = {(rel, func_name) for rel, _, func_name, _ in _scan_flagged_sites()}
    stale = _stale_allowlist_entries(_FUNCTION_ALLOWLIST, covered)

    assert stale == [], (
        "Stale _FUNCTION_ALLOWLIST entries — each exempts no currently-flagged "
        "handler, so it silently widens the fence's blind spot:\n  "
        + "\n  ".join(f"remove stale entry {entry}" for entry in stale)
        + "\n\nDelete each listed entry (with its justification comment). If a "
        "handler was narrowed or removed, its exemption is no longer warranted."
        "\n\nSee CLAUDE.md → 'Fail loud — no warning-silencing exception "
        "handlers'."
    )


# Consumes the same memoized whole-tree scan as the two gates above; keep the
# component budget so a cold cache stays green under suite load.
@pytest.mark.timeout(15)
def test_allowlist_entry_breadth_is_ratcheted() -> None:
    """Every allowlist entry exempts exactly its pinned number of handlers.

    The allowlist is a **whole-function** exemption, so a new silencer-shape
    handler added inside an already-listed function passes silently —
    ``test_allowlist_entries_exempt_a_live_handler`` only proves each entry
    shields *at least one* handler, never that it shields no *more* than it
    should. ``_ENTRY_HANDLER_BASELINE`` pins each entry's exempt-handler count,
    and this gate fails the moment the live count diverges, so exemption breadth
    can only move by a deliberate, reviewed baseline edit — the exempt surface
    only ever shrinks.
    """
    # A pin whose (path, function) is no longer in the allowlist bounds nothing
    # — _overbroad_allowlist_entries only iterates the allowlist — so it rots
    # silently, the symmetric blind spot _stale_allowlist_entries closes for the
    # allowlist. Gate it here so a dropped allowlist entry forces dropping its
    # pin in the same change.
    dead = _dead_baseline_pins(_ENTRY_HANDLER_BASELINE, _FUNCTION_ALLOWLIST)
    assert dead == [], (
        "_ENTRY_HANDLER_BASELINE pins whose (path, function) is no longer in "
        "_FUNCTION_ALLOWLIST — dead config _overbroad_allowlist_entries never "
        "checks (it iterates the allowlist):\n  "
        + "\n  ".join(dead)
        + "\n\nRemove each dead pin from _ENTRY_HANDLER_BASELINE (its allowlist "
        "entry was dropped), or restore the allowlist entry if it went missing "
        "by mistake.\n\nSee CLAUDE.md → 'Fail loud — no warning-silencing "
        "exception handlers'."
    )

    counts: dict[tuple[str, str], int] = {}
    for rel, _, func_name, _ in _scan_flagged_sites():
        counts[(rel, func_name)] = counts.get((rel, func_name), 0) + 1
    overbroad = _overbroad_allowlist_entries(
        _FUNCTION_ALLOWLIST, counts, _ENTRY_HANDLER_BASELINE,
    )

    assert overbroad == [], (
        "_FUNCTION_ALLOWLIST entries whose exempted-handler count no longer "
        "matches _ENTRY_HANDLER_BASELINE (breadth ratchet):\n  "
        + "\n  ".join(overbroad)
        + "\n\nFor each entry either:\n"
        + "  1. Fix the offending handler (re-raise / narrow / typed-failure "
        + "return) so the count returns to its pin — the exempt surface should "
        + "shrink, not grow\n"
        + "  2. Deliberately bump _ENTRY_HANDLER_BASELINE with a justification "
        + "for the reviewed change in breadth (found > pin), or lower the pin "
        + "in the same change when a handler was narrowed away (found < pin)\n"
        + "\nSee CLAUDE.md → 'Fail loud — no warning-silencing exception "
        + "handlers'."
    )


# Independent whole-tree AST walk (memoized); keep the component budget so a
# cold cache stays green under suite load, mirroring the gates above.
@pytest.mark.timeout(15)
def test_debug_level_swallow_sites_are_ratcheted() -> None:
    """The debug/info carve-out's site count may only shrink.

    ``_body_is_silencer`` excludes ``logger.{debug,info}`` swallows by design —
    a best-effort pattern resting on human review, not this fence. Nothing
    bounded that carve-out, so each new debug-level swallow landed green and
    unreviewed. ``_DEBUG_SWALLOW_BASELINE`` pins the per-file count, and this
    gate fails the moment a file diverges, so the carve-out moves only by a
    deliberate, reviewed baseline edit — the same ratchet
    ``test_allowlist_entry_breadth_is_ratcheted`` applies to allowlist breadth.
    """
    counts: dict[str, int] = {}
    for rel, _, _ in _scan_debug_swallow_sites():
        counts[rel] = counts.get(rel, 0) + 1

    drifting = {
        rel
        for rel in set(counts) | set(_DEBUG_SWALLOW_BASELINE)
        if counts.get(rel, 0) != _DEBUG_SWALLOW_BASELINE.get(rel, 0)
    }
    drift = sorted(
        f"{rel} expected {_DEBUG_SWALLOW_BASELINE.get(rel, 0)}, "
        f"found {counts.get(rel, 0)}"
        for rel in drifting
    )
    offenders = [
        f"{rel}:{lineno} (in {func_name})"
        for rel, lineno, func_name in _scan_debug_swallow_sites()
        if rel in drifting
    ]

    assert drift == [], (
        "Files whose debug/info-level broad-except swallow count no longer "
        "matches _DEBUG_SWALLOW_BASELINE (carve-out ratchet):\n  "
        + "\n  ".join(drift)
        + "\n\nCarve-out sites in the drifting files:\n  "
        + "\n  ".join(offenders)
        + "\n\nFor each file either:\n"
        + "  1. Surface the failure to the caller (re-raise / narrow the "
        + "exception / return a typed failure) so the count returns to its pin "
        + "— the carve-out should shrink, not grow\n"
        + "  2. Deliberately bump _DEBUG_SWALLOW_BASELINE in the same diff, "
        + "with a justification for the new best-effort swallow (found > pin), "
        + "or lower the pin when a swallow was removed (found < pin)\n"
        + "\nSee CLAUDE.md → 'Fail loud — no warning-silencing exception "
        + "handlers'."
    )


def test_debug_swallow_detector_synthetic_shapes() -> None:
    """``_body_is_debug_swallow`` matches only the carve-out shape.

    The disk scan above exercises the matching branch only; drive the helper
    over synthetic snippets to prove each rejection — a ``raise`` in the body, a
    loud-level call or a bare ``print`` alongside the debug one (both already
    the fence's business, so never double-pinned here), and a body with no
    logging at all.
    """

    def _handler(body: str) -> ast.ExceptHandler:
        source = f"try:\n    x = 1\nexcept Exception:\n{body}"
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.Try)
        return node.handlers[0]

    assert _body_is_debug_swallow(_handler("    logger.debug('e')")) is True
    assert _body_is_debug_swallow(_handler("    log.info('e')")) is True
    assert _body_is_debug_swallow(_handler("    logger.debug('e')\n    raise")) is False
    assert _body_is_debug_swallow(
        _handler("    logger.debug('e')\n    logger.warning('e')"),
    ) is False
    assert _body_is_debug_swallow(
        _handler("    logger.debug('e')\n    print('e')"),
    ) is False
    assert _body_is_debug_swallow(_handler("    pass")) is False
    assert _body_is_debug_swallow(_handler("    other.debug('e')")) is False
    # A conditional raise still falls through to the debug swallow (#4165).
    assert _body_is_debug_swallow(
        _handler("    if cond():\n        raise\n    logger.debug('e')"),
    ) is True


def test_stale_allowlist_entry_is_reported() -> None:
    """``_stale_allowlist_entries`` flags exactly the entries nothing covers.

    The real allowlist has no stale entries, so the disk scan above only ever
    exercises the passing branch. Drive the same helper over a synthetic
    ``(allowlist, covered)`` pair to prove the failure half — mirroring how the
    synthetic detector cases prove ``_detect_silencer_handlers``.
    """
    covered = {("maestro/agent.py", "shutdown"), ("maestro/crash.py", "handle_crash")}

    # A covered entry is not stale.
    assert _stale_allowlist_entries(
        frozenset({("maestro/agent.py", "shutdown")}), covered,
    ) == []
    # Uncovered entries are reported, sorted, one line each.
    assert _stale_allowlist_entries(
        frozenset({("maestro/zzz.py", "gone"), ("maestro/agent.py", "narrowed")}),
        covered,
    ) == ["maestro/agent.py::narrowed", "maestro/zzz.py::gone"]
    # Keying is path-qualified: same function name in a different module is
    # not covered by its sibling.
    assert _stale_allowlist_entries(
        frozenset({("maestro_review/agent.py", "shutdown")}), covered,
    ) == ["maestro_review/agent.py::shutdown"]


def test_overbroad_allowlist_entry_is_reported() -> None:
    """``_overbroad_allowlist_entries`` flags every entry off its pinned breadth.

    The real tree is green by construction, so the disk-backed ratchet only ever
    exercises the passing branch. Drive the helper over synthetic
    ``(allowlist, counts, baseline)`` triples to prove the failure half — a count
    matching its pin is silent; above **or** below its pin is reported; and an
    entry absent from the baseline is pinned at exactly ``1``.
    """
    allowlist = frozenset({
        ("m.py", "match"),
        ("m.py", "over"),
        ("m.py", "under"),
        ("m.py", "unlisted_ok"),
        ("m.py", "unlisted_grown"),
    })
    baseline = {
        ("m.py", "match"): 2,
        ("m.py", "over"): 2,
        ("m.py", "under"): 2,
    }
    counts = {
        ("m.py", "match"): 2,          # == pin → silent
        ("m.py", "over"): 3,           # > pin → reported
        ("m.py", "under"): 1,          # < pin → reported (narrowing must lower pin)
        ("m.py", "unlisted_ok"): 1,    # absent from baseline, pinned at 1 → silent
        ("m.py", "unlisted_grown"): 2,  # absent from baseline, pinned at 1 → reported
    }

    assert _overbroad_allowlist_entries(allowlist, counts, baseline) == [
        "m.py::over expected 2, found 3",
        "m.py::under expected 2, found 1",
        "m.py::unlisted_grown expected 1, found 2",
    ]

    # A fully matching set is silent.
    assert _overbroad_allowlist_entries(
        frozenset({("m.py", "match")}),
        {("m.py", "match"): 2},
        {("m.py", "match"): 2},
    ) == []


def test_dead_baseline_pin_is_reported() -> None:
    """``_dead_baseline_pins`` flags exactly the pins missing from the allowlist.

    The real baseline is a subset of the allowlist, so the disk-backed gate only
    exercises the passing (empty) branch. Drive the helper over a synthetic
    ``(baseline, allowlist)`` pair to prove the failure half — a pin whose entry
    is still in the allowlist is silent; a pin whose entry was dropped is
    reported, sorted, path-qualified.
    """
    allowlist = frozenset({("m.py", "live"), ("m.py", "also_live")})

    # A pin whose entry is still in the allowlist is not dead.
    assert _dead_baseline_pins({("m.py", "live"): 2}, allowlist) == []
    # Pins whose entries left the allowlist are reported, sorted; live ones are
    # left out.
    assert _dead_baseline_pins(
        {
            ("m.py", "live"): 2,
            ("m.py", "gone"): 3,
            ("z.py", "also_gone"): 2,
        },
        allowlist,
    ) == ["m.py::gone", "z.py::also_gone"]
    # Keying is path-qualified: same function name in a different module is a
    # distinct, dead pin.
    assert _dead_baseline_pins(
        {("other.py", "live"): 2}, allowlist,
    ) == ["other.py::live"]


# ── Sanity sub-tests: prove the detector itself works ─────────────────


def _flagged(source: str) -> list[tuple[int, _FlaggedNode]]:
    """Helper: parse *source* and return the detector's flagged list."""
    return _detect_silencer_handlers(ast.parse(source))


def test_synthetic_broad_except_with_log_and_no_raise_is_flagged() -> None:
    """The canonical anti-pattern: broad except + logger.warning + no raise."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        logger.warning('boom')\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_except_with_debug_log_is_not_flagged() -> None:
    """Debug-level swallows are out of scope by design — see ``_body_is_silencer``.

    ``logger.debug(..., exc_info=True)`` / ``logger.info(...)`` in a
    broad handler is a deliberate best-effort pattern (used across
    ``maestro/sdk.py``, ``maestro/email.py``, ``maestro/agent.py``) that
    the detector does not flag. Both levels are pinned with their own
    snippet so the boundary is fully covered: adding ``debug`` to
    ``log_attrs`` flips the ``source`` assertion and adding ``info``
    flips the ``source_info`` assertion, forcing any future broadening
    of either to be a conscious, reviewed change.
    """
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        logger.debug('err', exc_info=True)\n"
    )
    source_info = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        logger.info('err', exc_info=True)\n"
    )
    assert _flagged(source) == []
    assert _flagged(source_info) == []


def test_synthetic_broad_except_with_raise_is_allowed() -> None:
    """A broad except that re-raises (with no logging) is not flagged."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        raise\n"
    )
    assert _flagged(source) == []


def test_synthetic_broad_except_with_continue_is_flagged() -> None:
    """A broad except whose body is only ``continue`` is a silent swallow."""
    source = (
        "def f(x):\n"
        "    for i in x:\n"
        "        try:\n"
        "            y()\n"
        "        except Exception:\n"
        "            continue\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_except_with_break_is_flagged() -> None:
    """A broad except whose body is only ``break`` is a silent swallow."""
    source = (
        "def f(x):\n"
        "    for i in x:\n"
        "        try:\n"
        "            y()\n"
        "        except Exception:\n"
        "            break\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_except_with_pass_and_continue_is_flagged() -> None:
    """A mixed ``pass`` + ``continue`` body is still semantically empty."""
    source = (
        "def f(x):\n"
        "    for i in x:\n"
        "        try:\n"
        "            y()\n"
        "        except Exception:\n"
        "            pass\n"
        "            continue\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_except_with_return_none_is_not_flagged() -> None:
    """A ``return None`` exits the frame — not an empty-body silencer."""
    source = (
        "def f():\n"
        "    try:\n"
        "        y()\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert _flagged(source) == []


def test_synthetic_broad_except_with_logger_then_raise_is_allowed() -> None:
    """Log-and-rethrow (the desired shape) is not flagged."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        logger.exception('boom')\n"
        "        raise\n"
    )
    assert _flagged(source) == []


def test_specific_except_is_not_inspected() -> None:
    """Specific exceptions (e.g. ``KeyError``) are out of scope by design."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except KeyError:\n"
        "        pass\n"
    )
    assert _flagged(source) == []


def test_bare_except_with_pass_is_flagged() -> None:
    """Empty/``pass`` body on a bare except is flagged."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except:\n"
        "        pass\n"
    )
    assert len(_flagged(source)) == 1


def test_cancellederror_swallow_is_specific_and_allowed() -> None:
    """``asyncio.CancelledError`` is an attribute access — specific, not broad.

    The ``_notify_best_effort`` shape (``except asyncio.CancelledError:
    logger.warning(...)``) classifies as specific by Step 1 and is not
    inspected; no allowlist entry needed for the detector to permit it.
    """
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except asyncio.CancelledError:\n"
        "        logger.warning('cancelled')\n"
    )
    assert _flagged(source) == []


def test_tuple_with_exception_is_broad() -> None:
    """A tuple containing ``Exception`` is broad (and silencer-shape is flagged)."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except (ValueError, Exception):\n"
        "        logger.warning('boom')\n"
    )
    assert len(_flagged(source)) == 1


def test_tuple_of_specific_exceptions_is_not_broad() -> None:
    """A tuple of only-specific exceptions is not broad — out of scope."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except (ValueError, KeyError):\n"
        "        logger.warning('boom')\n"
    )
    assert _flagged(source) == []


def test_typed_return_on_broad_except_is_allowed() -> None:
    """Broad except with a typed-return (no logger, no raise) is allowed.

    The failure surfaces structurally to the caller via the return
    type, so the detector does not flag this shape — only
    log-and-fall-through and empty/pass/bare continue/break are silencers.
    """
    source = (
        "def f():\n"
        "    try:\n"
        "        return True\n"
        "    except Exception:\n"
        "        return False, 'msg'\n"
    )
    assert _flagged(source) == []


# ── Broad ``contextlib.suppress`` pins (#3189) ────────────────────────
#
# ``with contextlib.suppress(Exception):`` is ``except Exception: pass`` by
# construction, so the detector flags a broad ``suppress`` on breadth alone —
# there is no body-shape second gate (see ``_is_broad_suppress`` /
# ``_detect_silencer_handlers``). These pins keep that a conscious rule. Each
# flagged-side pin returned ``[]`` against the pre-#3189 detector (which walked
# ``ast.ExceptHandler`` nodes only), so they also pin that the blind spot is
# closed.


def test_synthetic_broad_suppress_is_flagged() -> None:
    """The canonical evasion: ``with contextlib.suppress(Exception): ...``."""
    source = (
        "def f():\n"
        "    with contextlib.suppress(Exception):\n"
        "        x()\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_bare_suppress_baseexception_is_flagged() -> None:
    """The bare from-imported ``suppress(BaseException)`` spelling is broad too."""
    source = (
        "def f():\n"
        "    with suppress(BaseException):\n"
        "        x()\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_suppress_varargs_with_exception_is_flagged() -> None:
    """``suppress(TimeoutError, Exception)`` is broad — the varargs analogue of
    the ``except (…, Exception)`` tuple case."""
    source = (
        "def f():\n"
        "    with contextlib.suppress(TimeoutError, Exception):\n"
        "        x()\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_async_with_broad_suppress_is_flagged() -> None:
    """``async with`` carrying a broad ``suppress`` item is flagged."""
    source = (
        "async def f():\n"
        "    async with contextlib.suppress(Exception):\n"
        "        x()\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_narrow_suppress_is_not_flagged() -> None:
    """The recommended narrow forms (specific types) are not flagged."""
    source_vcs = (
        "def f():\n"
        "    with contextlib.suppress(VCSError):\n"
        "        x()\n"
    )
    source_tuple = (
        "def f():\n"
        "    with contextlib.suppress(ValueError, TypeError):\n"
        "        x()\n"
    )
    assert _flagged(source_vcs) == []
    assert _flagged(source_tuple) == []


def test_synthetic_broad_suppress_gated_on_breadth_alone() -> None:
    """Breadth alone flags a broad ``suppress``: its body neither logs nor
    passes, yet it is flagged — unlike an ``except`` handler there is no
    body-shape escape (a typed-return does not un-silence a ``suppress``)."""
    source = (
        "def f():\n"
        "    with contextlib.suppress(Exception):\n"
        "        do_real_work()\n"
        "        return compute()\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_suppress_lineno_keys_enclosing_function() -> None:
    """A broad ``suppress`` inside a nested ``def`` reports the ``with``
    statement's own ``lineno``, which ``_line_to_function`` keys to the
    enclosing top-level function (closures resolve to their outer function),
    so the allowlist keys the whole enclosing function."""
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        with contextlib.suppress(Exception):\n"
        "            x()\n"
    )
    flagged = _flagged(source)
    assert len(flagged) == 1
    lineno, node = flagged[0]
    assert isinstance(node, ast.With)
    line_to_func = _line_to_function(ast.parse(source))
    assert line_to_func[lineno] == "outer"


def test_describe_renders_broad_suppress_clause() -> None:
    """``_describe`` renders a flagged ``suppress`` ``with`` node as its
    offending ``suppress(...)`` call — the violator line the fence prints.

    This is the *only* direct exercise of ``_describe``'s ``ast.With`` branch:
    the other broad-suppress pins call ``_flagged`` (never ``_describe``), and
    every real broad-suppress site is allowlisted, so the fence filters it out
    before ``_describe`` runs. Without this pin a regression there (an
    ``IndexError`` on ``offending[0]`` or a format drift) would surface only the
    day a real un-allowlisted broad suppress lands.
    """
    _, node = _flagged(
        "def f():\n"
        "    with contextlib.suppress(Exception):\n"
        "        x()\n"
    )[0]
    assert _describe(node) == "with contextlib.suppress(Exception)"

    # The bare from-imported spelling renders without the ``contextlib.`` prefix.
    _, bare_node = _flagged(
        "def f():\n"
        "    with suppress(BaseException):\n"
        "        x()\n"
    )[0]
    assert _describe(bare_node) == "with suppress(BaseException)"

    # With several context managers, only the broad ``suppress`` item is
    # rendered even when it is not first — the ``offending`` comprehension
    # filters the non-suppress item out before ``offending[0]``.
    _, multi_node = _flagged(
        "def f():\n"
        "    with open('p') as fh, contextlib.suppress(Exception):\n"
        "        x()\n"
    )[0]
    assert _describe(multi_node) == "with contextlib.suppress(Exception)"


def test_synthetic_broad_except_with_conditional_raise_is_flagged() -> None:
    """A ``raise`` guarded by an ``if`` lets every other exception fall through.

    ``if rare(exc): raise`` then ``logger.exception(...)`` logs-and-swallows
    all but the rare case, so the raise must not exempt the handler (#4165).
    """
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception as exc:\n"
        "        if cond(exc):\n"
        "            raise\n"
        "        logger.exception('boom')\n"
    )
    assert len(_flagged(source)) == 1


def test_synthetic_broad_except_with_if_else_both_raising_is_allowed() -> None:
    """Every path ends in ``raise`` — both ``if`` arms, or a ``with`` body."""
    source = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception as exc:\n"
        "        logger.warning('boom')\n"
        "        if cond(exc):\n"
        "            raise\n"
        "        else:\n"
        "            raise ValueError('x') from exc\n"
    )
    source_with = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        with lock:\n"
        "            logger.warning('boom')\n"
        "            raise\n"
    )
    assert _flagged(source) == []
    assert _flagged(source_with) == []


def test_synthetic_broad_except_raise_inside_nested_try_or_loop_is_flagged() -> None:
    """A ``raise`` nested only in a loop or inner ``try`` may fall through."""
    source_loop = (
        "def f(xs):\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        for x in xs:\n"
        "            raise\n"
        "        logger.error('boom')\n"
    )
    source_try = (
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        logger.error('boom')\n"
        "        try:\n"
        "            raise\n"
        "        except ValueError:\n"
        "            pass\n"
    )
    assert len(_flagged(source_loop)) == 1
    assert len(_flagged(source_try)) == 1
