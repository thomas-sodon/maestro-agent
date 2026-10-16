"""Execution pipeline for approved publication proposals (proposal 033).

Consumes :class:`PublicationProposal` rows in ``approved`` status emitted
by proposal 032 and turns each into a public GitHub repository: stage the
publishable files into a fresh directory, init a git repo, create the
remote via ``gh repo create``, push, and mark the proposal ``published``.

Design seams that make this safe to run autonomously:

* **TOCTOU-safe ``approved → executing`` flip** — :meth:`_publish` calls
  ``Memory.update_proposal_status`` with ``expect_status="approved"``
  under file lock; a second concurrent tick or surface that races for
  the same proposal exits cleanly.
* **Durable ``published_url`` checkpoint** — once ``gh repo create``
  returns, the URL is persisted before any ``git push`` is attempted.
  A crash after this checkpoint routes the proposal through
  :meth:`_resume_publish` next tick instead of double-creating the repo.
* **Per-file hard-block scan at publish time** — every file named in
  ``proposal.publishable_files`` is re-read and re-scanned in full
  (the LLM review only saw a head-truncated bundle). Any hard-block
  finding aborts before any remote action.
* **One-fresh-publish-per-tick budget** — :meth:`tick` reconciles all
  stale/resumable rows but only attempts one *new* approved proposal
  per call so a single bug bounds the blast radius to one repo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import Memory, PublicationProposal
from maestro.notifications.base import Notifier
from maestro.secret_scan import scan_contents
from maestro.vcs import CodeHost, VCSError

logger = logging.getLogger("maestro")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PublicationBlockedError(Exception):
    """Raised when a publish-time secret scan or guard rejects content.

    Surfaces a category-prefixed reason (``"secret_scan_blocked:..."``,
    ``"binary_in_publishable_files:..."``) so the persisted ``error``
    field on the failed proposal is grep-friendly.
    """


class PublicationFailedError(Exception):
    """Raised for non-blocking pipeline failures (git, gh, IO).

    The Publisher catches this internally and persists the message to
    ``proposal.error``; callers should not need to catch it themselves.
    """


_RESERVED_FUNDING_PATHS = frozenset(
    {"funding.yml", "docs/funding.yml", ".github/funding.yml"},
)


def _is_reserved_funding_path(rel: str) -> bool:
    """True when *rel* names a ``FUNDING.yml`` GitHub may render.

    GitHub resolves community-health files from the repo root, ``docs/``,
    and ``.github/``, so all three are reserved (case-insensitive).
    """
    parts = [p for p in PurePosixPath(rel).parts if p != "."]
    return "/".join(parts).lower() in _RESERVED_FUNDING_PATHS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_FIFO_SORT_SENTINEL = "9999-12-31T23:59:59+00:00"

_GIT_BOT_EMAIL = "maestro-bot@example.com"
_GIT_BOT_NAME = "Maestro"

# Subprocess timeouts.  Without these a hung ``gh`` (e.g. waiting on
# stdin auth) or ``git push`` would block the loop indefinitely past
# Docker's SIGTERM grace period, leaving an orphan child after SIGKILL.
# Mirrors :class:`SubprocessVCS`'s defaults.
_GIT_TIMEOUT_SECONDS = 30
_GIT_PUSH_TIMEOUT_SECONDS = 120

_DEFAULT_GITIGNORE = """\
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
.env
.pytest_cache/
.mypy_cache/
.ruff_cache/
"""


def _format_license_mit(year: int, holder: str) -> str:
    return (
        f"MIT License\n\n"
        f"Copyright (c) {year} {holder}\n\n"
        "Permission is hereby granted, free of charge, to any person "
        "obtaining a copy\nof this software and associated documentation "
        "files (the \"Software\"), to deal\nin the Software without "
        "restriction, including without limitation the rights\nto use, "
        "copy, modify, merge, publish, distribute, sublicense, and/or "
        "sell\ncopies of the Software, and to permit persons to whom the "
        "Software is\nfurnished to do so, subject to the following "
        "conditions:\n\n"
        "The above copyright notice and this permission notice shall be "
        "included in\nall copies or substantial portions of the Software.\n\n"
        "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, "
        "EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES "
        "OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND "
        "NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT "
        "HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, "
        "WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING "
        "FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR "
        "OTHER DEALINGS IN\nTHE SOFTWARE.\n"
    )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


@dataclass
class _PublishContext:
    """Per-publish bookkeeping shared between staging and the pipeline."""

    proposal: PublicationProposal
    staging_dir: Path


class Publisher:
    """Drives the ``approved → executing → published`` state machine.

    Construct fresh per :meth:`tick`; the class holds no per-iteration
    state beyond its dependencies and the injected ``clock`` / ``runner``.
    """

    def __init__(
        self,
        memory: Memory,
        journal: Journal,
        notifier: Notifier,
        host: CodeHost,
        config: Config,
        *,
        clock: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._memory = memory
        self._journal = journal
        self._notifier = notifier
        self._host = host
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runner = runner or subprocess.run
        self._shutdown_event = shutdown_event

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Run one publication-execution pass.

        Four ordered passes so a stuck row cannot starve fresh work:

        1. **Stale-executing reconcile** — any proposal in ``executing``
           with no ``published_url`` and ``executing_started_at`` older
           than the configured stale threshold is downgraded to
           ``failed`` so the queue isn't permanently blocked by a
           crashed pre-create attempt.
        2. **Resume after checkpoint** — any proposal in ``executing``
           *with* a ``published_url`` set runs through
           :meth:`_resume_publish` to attempt the post-create push tail.
        3. **Resolve rejected Telegram buttons** — dashboard-initiated
           rejects leave any pending Telegram inline-button message
           live; this pass asks the notifier to edit each rejected
           proposal's message to ``--- Rejected ---``. Idempotent: the
           notifier's per-process pending-message dict is the natural
           flag (popped → no-op on re-tick).
        4. **Fresh publish (capped at one)** — pick the FIFO-by-
           ``reviewed_at`` ``approved`` proposal and run :meth:`_publish`
           on it. Bounded to one per tick so any bug here can damage at
           most one repo per iteration.

        Shutdown gating: a set ``shutdown_event`` short-circuits between
        passes so a SIGTERM cannot start a fresh ``gh repo create`` it
        won't be able to finish before the Docker grace period expires.
        Resume, reconcile, and resolve-rejected are idempotent and may
        complete.
        """
        if self._shutdown_set():
            return
        await self._reconcile_stale_executing()
        if self._shutdown_set():
            return
        await self._resume_executing_after_checkpoint()
        if self._shutdown_set():
            return
        await self._resolve_rejected_publications()
        if self._shutdown_set():
            return
        await self._publish_one_approved()

    def _shutdown_set(self) -> bool:
        return self._shutdown_event is not None and self._shutdown_event.is_set()

    # ------------------------------------------------------------------
    # Pass A — stale-executing reconcile
    # ------------------------------------------------------------------

    async def _reconcile_stale_executing(self) -> None:
        threshold = float(self._config.publication_executing_stale_timeout_seconds)
        now = self._clock()
        for proposal in self._memory.get_publication_proposals(
            status_filter="executing",
        ):
            if proposal.published_url:
                # Has a checkpoint - handled by the resume pass.
                continue
            started = _parse_iso(proposal.executing_started_at)
            if started is None:
                # Missing timestamp on an executing row - treat as stale
                # immediately rather than leaving it stuck forever.
                age: float = threshold + 1.0
            else:
                age = (now - started).total_seconds()
            if age <= threshold:
                continue
            # Step 6 (``create_public_repo``) and Step 7 (the
            # ``published_url`` checkpoint) of ``_publish`` are separate
            # steps; a crash in that window leaves the row
            # ``executing`` + ``published_url=None`` while the public repo
            # already exists on GitHub — the orphan the Step-7 race branch
            # documents. Probe for it before labelling: stamping
            # ``stale_executing_pre_create`` on a post-create crash lies
            # (triage reads "never created a repo" and never deletes the
            # orphan). Resolve the owner exactly as Step 6 does.
            owner = (
                self._config.publication_owner
                or self._host.resolve_owner()
            )
            if owner and self._host.repo_exists(
                owner, proposal.proposed_name,
            ):
                # Name existence is a *proxy* for "this proposal created the
                # repo in Step 6", not proof of ownership: this reconcile
                # fires for any pre-checkpoint stale row (a crash anywhere in
                # Steps 1-7), so a live same-named repo could in principle
                # predate this proposal. It is a safe proxy under the
                # configured owner because the name was verified free at
                # emission time — ``artifact_review`` downgrades a proposal
                # to ``improve`` on a ``repo_exists`` collision, so a name
                # only reaches ``executing`` if it was unclaimed then; a live
                # repo is therefore almost always this proposal's own Step-6
                # orphan. The residual case — a pre-Step-6 crash whose name
                # now collides with a concurrent same-name proposal or an
                # externally-created repo — is deliberately out of scope (see
                # the #1410 fix plan): the resume push targets our own owner
                # and is non-force (``git push -u origin main``), so the worst
                # case is a rejected non-fast-forward push, not a clobber.
                # The repo is live — reconstruct ``create_public_repo``'s
                # ``https://github.com/{owner}/{name}`` URL contract,
                # checkpoint it (mirroring Step 7), and hand the row to the
                # resume pass instead of failing it. ``repo_exists``
                # returning ``False`` on an inconclusive probe is the
                # conservative fallback (keeps today's loud label), not a
                # silent success.
                url = f"https://github.com/{owner}/{proposal.proposed_name}"
                if not self._memory.update_proposal_status(
                    proposal.proposal_id,
                    "executing",
                    expect_status="executing",
                    published_url=url,
                ):
                    # A racing surface (Telegram / dashboard) flipped the
                    # row out of ``executing`` between the read above and
                    # this checkpoint; that surface owns the row now. Don't
                    # claim we routed to resume, and don't fall through to
                    # the stale-fail path below — it also expects
                    # ``executing`` and would no-op. Mirrors Step 7.
                    logger.warning(
                        "publication: proposal %s (%s) no longer executing "
                        "at reconcile checkpoint; skipping",
                        proposal.proposal_id, proposal.proposed_name,
                    )
                    continue
                logger.warning(
                    "publication: proposal %s (%s) crashed post-create "
                    "pre-checkpoint; repo exists, routing to resume "
                    "(url=%s)",
                    proposal.proposal_id, proposal.proposed_name, url,
                )
                continue
            if not self._memory.update_proposal_status(
                proposal.proposal_id,
                "failed",
                expect_status="executing",
                error="stale_executing_pre_create",
            ):
                # Same race as the reconcile checkpoint above: a surface
                # flipped the row out of ``executing`` between the read
                # and this fail write, so the winning surface owns the
                # row — and its staging tree. Don't wipe staging or send
                # a "timed out" notification for a state the row no
                # longer holds. Mirrors Step 7 and the resume branch.
                logger.warning(
                    "publication: proposal %s (%s) no longer executing "
                    "at reconcile fail; skipping",
                    proposal.proposal_id, proposal.proposed_name,
                )
                continue
            logger.warning(
                "publication: proposal %s (%s) timed out in executing "
                "(age=%.0fs threshold=%ds)",
                proposal.proposal_id, proposal.proposed_name, age, threshold,
            )
            shutil.rmtree(
                self._staging_dir_for(proposal.proposal_id),
                ignore_errors=True,
            )
            await self._notify_failure(
                proposal, "timed out in executing pre-create",
            )

    # ------------------------------------------------------------------
    # Pass B — resume after checkpoint
    # ------------------------------------------------------------------

    async def _resume_executing_after_checkpoint(self) -> None:
        for proposal in self._memory.get_publication_proposals(
            status_filter="executing",
        ):
            if not proposal.published_url:
                continue
            await self._resume_publish(proposal)

    # ------------------------------------------------------------------
    # Pass C — resolve rejected Telegram buttons
    # ------------------------------------------------------------------

    async def _resolve_rejected_publications(self) -> None:
        """Clear Telegram inline buttons for proposals rejected via the dashboard.

        The Telegram-initiated reject path is already self-clearing
        through ``_update_message_after_action`` in
        ``maestro/notifications/telegram.py``. The dashboard-initiated
        reject path leaves ``_publication_messages`` populated on the
        loop's notifier, so this pass iterates rejected proposals each
        tick and asks the notifier to resolve them.
        ``TelegramNotifier.resolve_publication`` early-returns when the
        dict has no entry for the proposal id, so re-iterating the same
        rejected rows across ticks costs at most one dict lookup per
        proposal — the dict itself is the natural idempotency flag.
        """
        for proposal in self._memory.get_publication_proposals(
            status_filter="rejected",
        ):
            try:
                await self._notifier.resolve_publication(
                    proposal.proposal_id, "Rejected",
                )
            except Exception:
                logger.warning(
                    "publication: failed to resolve Telegram buttons "
                    "for rejected proposal %s",
                    proposal.proposal_id, exc_info=True,
                )

    # ------------------------------------------------------------------
    # Pass D — fresh publish (one per tick)
    # ------------------------------------------------------------------

    async def _publish_one_approved(self) -> None:
        approved = self._memory.get_publication_proposals(
            status_filter="approved",
        )
        if not approved:
            return
        # FIFO by reviewed_at (the moment Thomas approved). Fallback to
        # created_at then proposal_id for deterministic ordering when
        # reviewed_at is unset on a legacy row. Undated legacy rows use
        # a far-future sentinel so they sort *last* — otherwise the
        # empty-string fallback would sort before any real timestamp,
        # starving properly-dated proposals.
        approved.sort(
            key=lambda p: (
                p.reviewed_at or p.created_at or _FIFO_SORT_SENTINEL,
                p.proposal_id,
            ),
        )
        await self._publish(approved[0])

    # ------------------------------------------------------------------
    # Core publish pipeline
    # ------------------------------------------------------------------

    async def _publish(self, proposal: PublicationProposal) -> None:
        """Run the ten-step publish pipeline for *proposal*.

        Steps 1-6 are pre-create (rollback = mark failed and bail).
        Step 7 is the durable checkpoint after the public repo exists
        on GitHub. Steps 8-10 are the resumable tail; a crash here
        leaves the proposal in ``executing`` + ``published_url`` so
        the next tick's resume pass can finish the push.
        """
        proposal_id = proposal.proposal_id
        started_at = self._clock().isoformat()

        # Step 1: TOCTOU-safe approved → executing flip.
        if not self._memory.update_proposal_status(
            proposal_id, "executing",
            expect_status="approved",
            executing_started_at=started_at,
        ):
            logger.info(
                "publication: proposal %s lost the approved→executing "
                "race; skipping",
                proposal_id,
            )
            return

        # Clear any still-pending Telegram inline-button message before
        # publish work begins — the dashboard may have approved this
        # proposal while the Telegram button is still showing
        # Approve/Reject. Mirror of agent._implement_approved_plans'
        # pre-implementation resolve_plan call for evolution plans.
        try:
            await self._notifier.resolve_publication(proposal_id, "Approved")
        except Exception:
            logger.warning(
                "publication: failed to resolve Telegram buttons for "
                "approved proposal %s",
                proposal_id, exc_info=True,
            )

        # Step 2: resolve the source artifacts directory.
        task = self._memory.get_task(proposal.task_id)
        if task is None or not task.artifacts_dir:
            await self._fail(
                proposal_id, "artifacts_missing",
                f"task {proposal.task_id} not found or has no artifacts_dir",
            )
            return
        artifacts_dir = Path(task.artifacts_dir)
        if not artifacts_dir.is_dir():
            await self._fail(
                proposal_id, "artifacts_missing",
                f"artifacts_dir {artifacts_dir} is not a directory",
            )
            return

        # Step 3: prepare staging dir.  Remove any leftover staging
        # directory from a prior failed attempt so ``git add -A`` in
        # step 5 cannot pick up stale unscanned files.
        staging = self._staging_dir_for(proposal_id)
        try:
            if staging.is_dir():
                shutil.rmtree(staging)
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            await self._fail(
                proposal_id, "staging_mkdir_failed", str(exc),
            )
            return

        # Step 4: stage files (full secret scan happens here).
        try:
            redact_findings = self._stage(proposal, artifacts_dir, staging)
        except PublicationBlockedError as exc:
            await self._fail(
                proposal_id, str(exc),
                f"publish-time scan blocked publication: {exc}",
            )
            return
        except OSError as exc:
            await self._fail(
                proposal_id, "stage_io_failed", str(exc),
            )
            return

        # Step 5: git init + initial commit.
        try:
            self._git_init_commit(staging, proposal.task_id)
        except PublicationFailedError as exc:
            await self._fail(
                proposal_id, "git_init_commit_failed", str(exc),
            )
            return

        # Step 6: create the public repo on GitHub.
        owner = (
            self._config.publication_owner
            or self._host.resolve_owner()
        )
        if not owner:
            await self._fail(
                proposal_id, "owner_unresolvable",
                "no publication owner configured or resolvable",
            )
            return
        try:
            url = self._host.create_public_repo(
                proposal.proposed_name,
                proposal.description,
                owner=owner,
            )
        except VCSError as exc:
            await self._fail(
                proposal_id, "create_public_repo_failed", str(exc),
            )
            return

        # Step 7: durable checkpoint — persist published_url.
        merged_findings = list(proposal.scrub_findings) + redact_findings
        if not self._memory.update_proposal_status(
            proposal_id, "executing",
            expect_status="executing",
            published_url=url,
            scrub_findings=merged_findings,
        ):
            # A racing surface (Telegram / dashboard) flipped the row out
            # of ``executing`` between Step 1 and here. Abort the push
            # without calling ``_fail`` — ``_fail`` also expects
            # ``executing`` and would no-op against the new state; the
            # surface that won the race owns the row. ``published_url``
            # stays unset on this proposal; the orphaned ``gh repo create``
            # surfaces only as the public repo itself.
            logger.warning(
                "publication: proposal %s no longer executing at step-7 "
                "checkpoint; aborting push",
                proposal_id,
            )
            return

        # Step 8-10: configure remote and push (resumable tail).
        remote_proposal = self._memory.get_proposal(proposal_id)
        if remote_proposal is None:
            # Vanished after checkpoint — treat as resumable so the
            # next tick can re-read and finish.
            return
        await self._push_and_finalize(remote_proposal, staging)

    # ------------------------------------------------------------------
    # Resume path
    # ------------------------------------------------------------------

    async def _resume_publish(self, proposal: PublicationProposal) -> None:
        """Finish (or hand off) a proposal that crashed after the checkpoint.

        Runs the post-create tail with a per-tick push-retry budget.
        Exhaustion flips ``status=failed`` but **keeps** ``published_url``
        so Thomas can find the orphaned repo and clean it up.
        """
        proposal_id = proposal.proposal_id
        url = proposal.published_url or ""
        if not url:
            # Should not happen — caller already filtered.
            return

        staging = self._staging_dir_for(proposal_id)
        if not staging.is_dir():
            await self._fail(
                proposal_id,
                "staging_missing_after_repo_create",
                f"staging dir {staging} missing after gh repo create — "
                f"public repo {url} is orphaned",
                clear_published_url=False,
            )
            return
        await self._push_and_finalize(proposal, staging)

    # ------------------------------------------------------------------
    # Push + finalize (shared by fresh + resume)
    # ------------------------------------------------------------------

    async def _push_and_finalize(
        self, proposal: PublicationProposal, staging: Path,
    ) -> None:
        url = proposal.published_url or ""
        proposal_id = proposal.proposal_id
        try:
            self._set_origin_remote(staging, url)
        except PublicationFailedError as exc:
            await self._fail(
                proposal_id,
                f"repo_created_but_push_failed:set_remote:{exc}",
                f"failed to configure origin remote: {exc}",
                clear_published_url=False,
            )
            return

        last_err: str = ""
        for attempt in range(1, self._config.publication_push_retry_limit + 1):
            try:
                self._git_push(staging)
            except PublicationFailedError as exc:
                last_err = str(exc)
                logger.warning(
                    "publication: push attempt %d/%d failed for proposal "
                    "%s: %s",
                    attempt,
                    self._config.publication_push_retry_limit,
                    proposal_id,
                    last_err,
                )
                continue
            if not self._memory.update_proposal_status(
                proposal_id, "published",
                expect_status="executing",
            ):
                # A racing surface (Telegram / dashboard) legitimately
                # flipped the row to ``failed`` / ``rejected`` between the
                # Step 7 checkpoint and this successful push. The winning
                # surface owns the row — do not stomp it back to
                # ``published`` and do not fire a phantom success
                # notification. Mirrors the Step 7 checkpoint's race-lost
                # branch.
                logger.warning(
                    "publication: proposal %s no longer executing at "
                    "publish finalize; skipping",
                    proposal_id,
                )
                return
            await self._notify_success(proposal, url)
            return
        await self._fail(
            proposal_id,
            f"repo_created_but_push_failed:{last_err}",
            f"push exhausted after {self._config.publication_push_retry_limit} "
            f"attempts: {last_err}",
            clear_published_url=False,
        )

    # ------------------------------------------------------------------
    # Stage
    # ------------------------------------------------------------------

    def _stage(
        self,
        proposal: PublicationProposal,
        artifacts_dir: Path,
        staging_dir: Path,
    ) -> list[str]:
        """Stage every file in ``proposal.publishable_files`` into *staging_dir*.

        Re-reads each file in full (the LLM review saw only a
        head-truncated bundle) and runs the publish-time secret scan.
        Hard-block findings raise :class:`PublicationBlockedError` and
        the caller marks the proposal failed without touching GitHub.
        Returns the aggregate redact findings so they can be merged
        into ``proposal.scrub_findings`` at the checkpoint.
        """
        # Backstop (issue #1417): a proposal persisted before the
        # emission-stage name/description guard existed could still carry
        # a hard-block secret in its repo name or description — both flow
        # straight into ``create_public_repo``. Block here before any git
        # or GitHub work, mirroring the per-file hard-block branch below.
        for meta_value in (proposal.proposed_name, proposal.description):
            meta_scan = scan_contents(meta_value)
            if meta_scan.hard_block_findings:
                raise PublicationBlockedError(
                    f"secret_scan_blocked:{meta_scan.hard_block_findings[0]}",
                )

        all_redacts: list[str] = []
        any_python = False
        artifacts_root = artifacts_dir.resolve(strict=False)
        staging_root = staging_dir.resolve(strict=False)
        for rel in proposal.publishable_files:
            if _is_reserved_funding_path(rel):
                # A published repo never carries a FUNDING.yml: an
                # artifact's own copy is dropped at staging.
                logger.info(
                    "Publication %s: skipping artifact-supplied %s",
                    proposal.proposal_id, rel,
                )
                continue
            src = artifacts_dir / rel
            dest = staging_dir / rel
            try:
                src_resolved = src.resolve(strict=False)
                dest_resolved = dest.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise PublicationBlockedError(
                    f"unsafe_path_in_publishable_files:{rel}",
                ) from exc
            if not src_resolved.is_relative_to(artifacts_root):
                raise PublicationBlockedError(
                    f"unsafe_path_in_publishable_files:{rel}",
                )
            if not dest_resolved.is_relative_to(staging_root):
                raise PublicationBlockedError(
                    f"unsafe_path_in_publishable_files:{rel}",
                )
            try:
                content = src.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise PublicationBlockedError(
                    f"binary_in_publishable_files:{rel}",
                ) from exc
            scan = scan_contents(content)
            if scan.hard_block_findings:
                first = scan.hard_block_findings[0]
                raise PublicationBlockedError(
                    f"secret_scan_blocked:{first}",
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(scan.scrubbed_content, encoding="utf-8")
            all_redacts.extend(scan.redact_findings)
            if rel.endswith(".py"):
                any_python = True

        # Project metadata.
        year = self._clock().year
        license_text = _format_license_mit(
            year, self._config.publication_copyright_holder,
        )
        (staging_dir / "LICENSE").write_text(license_text, encoding="utf-8")

        readme = proposal.proposed_readme or f"# {proposal.proposed_name}\n"
        (staging_dir / "README.md").write_text(readme, encoding="utf-8")

        if any_python:
            gitignore_path = staging_dir / ".gitignore"
            if not gitignore_path.exists():
                gitignore_path.write_text(
                    _DEFAULT_GITIGNORE, encoding="utf-8",
                )
        return all_redacts

    # ------------------------------------------------------------------
    # Git plumbing
    # ------------------------------------------------------------------

    def _git_init_commit(self, staging_dir: Path, task_id: str) -> None:
        """Init *staging_dir* as a git repo and make the first commit.

        Uses ``-c user.email=… -c user.name=…`` per invocation rather
        than mutating any persistent git config so the staging directory
        carries no user-identifying state. The bot identity is
        intentionally distinct from Thomas's so commit history reads as
        an automated publication.
        """
        commands: list[list[str]] = [
            ["git", "init", "-b", "main"],
            ["git", "add", "-A"],
            [
                "git",
                "-c", f"user.email={_GIT_BOT_EMAIL}",
                "-c", f"user.name={_GIT_BOT_NAME}",
                "-c", "commit.gpgsign=false",
                "commit",
                "-m", f"Initial publication for task {task_id}",
            ],
        ]
        for cmd in commands:
            self._run_git(cmd, staging_dir)

    def _set_origin_remote(self, staging_dir: Path, url: str) -> None:
        """Configure ``origin`` to point at *url*.

        ``git remote set-url`` is idempotent against an already-configured
        remote, but fails when no ``origin`` exists. Try ``add`` first
        and fall back to ``set-url`` so this works on both fresh and
        resumed publishes.

        The fallback is conditional on the already-exists marker: only a
        ``git remote add`` failure whose stderr says the remote already
        exists (the legitimate resume case) falls back to ``set-url``. Any
        other failure — permission error, missing ``.git`` directory,
        invalid URL — re-raises the original error so the persisted
        proposal message names the true cause, not the fallback command.
        """
        try:
            self._run_git(
                ["git", "remote", "add", "origin", url], staging_dir,
            )
        except PublicationFailedError as exc:
            if "already exists" not in str(exc):
                raise
            self._run_git(
                ["git", "remote", "set-url", "origin", url], staging_dir,
            )

    def _git_push(self, staging_dir: Path) -> None:
        self._run_git(
            ["git", "push", "-u", "origin", "main"], staging_dir,
            timeout=_GIT_PUSH_TIMEOUT_SECONDS,
        )

    def _run_git(
        self,
        cmd: list[str],
        cwd: Path,
        *,
        timeout: int = _GIT_TIMEOUT_SECONDS,
    ) -> None:
        try:
            self._runner(
                cmd,
                cwd=str(cwd),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                # Pin the C locale so git stderr is deterministic English.
                # ``_set_origin_remote`` gates its ``set-url`` fallback on the
                # literal "already exists" marker; a localized stderr under a
                # non-English deployment locale would miss it and turn a
                # recoverable resumed publish into a permanent failure. Extend
                # (not replace) os.environ so push auth still reaches git.
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() if isinstance(
                exc.stderr, str,
            ) else ""
            raise PublicationFailedError(
                f"{' '.join(cmd[:3])} exited {exc.returncode}: {stderr}",
            ) from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise PublicationFailedError(
                f"{' '.join(cmd[:3])} failed: {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # Failure / success bookkeeping
    # ------------------------------------------------------------------

    async def _fail(
        self,
        proposal_id: str,
        error: str,
        log_detail: str,
        *,
        clear_published_url: bool = True,
    ) -> None:
        """Mark *proposal_id* failed and emit telemetry.

        ``clear_published_url=False`` keeps a checkpointed URL on the
        record so Thomas can find an orphaned repo for cleanup; the
        default clears it because pre-checkpoint failures haven't
        created anything on GitHub yet.

        Pre-create failures (``clear_published_url=True``) also wipe
        the staging dir so failed proposals don't leave orphan trees
        under ``<memory_dir>/publications/``. Post-create failures
        preserve staging so a manual recovery push is still possible.
        """
        kwargs: dict[str, Any] = {"error": error}
        if clear_published_url:
            kwargs["published_url"] = None
        self._memory.update_proposal_status(
            proposal_id, "failed",
            expect_status="executing",
            **kwargs,
        )
        logger.warning(
            "publication: proposal %s failed (%s) — %s",
            proposal_id, error, log_detail,
        )
        if clear_published_url:
            shutil.rmtree(
                self._staging_dir_for(proposal_id), ignore_errors=True,
            )
        proposal = self._memory.get_proposal(proposal_id)
        if proposal is not None:
            await self._notify_failure(proposal, error)

    async def _notify_success(
        self, proposal: PublicationProposal, url: str,
    ) -> None:
        try:
            self._journal.record_event(
                content=(
                    f"Published {proposal.proposed_name} ({url}) — "
                    f"proposal {proposal.proposal_id}"
                ),
                entry_type="milestone",
                triggered_by=f"publication:{proposal.proposal_id}",
            )
        except Exception:
            logger.warning(
                "publication: failed to journal success for proposal %s",
                proposal.proposal_id, exc_info=True,
            )
        try:
            await self._notifier.notify(
                f"Published {proposal.proposed_name}: {url}",
            )
        except Exception:
            logger.warning(
                "publication: success notification failed for proposal %s",
                proposal.proposal_id, exc_info=True,
            )

    async def _notify_failure(
        self, proposal: PublicationProposal, error: str,
    ) -> None:
        try:
            await self._notifier.notify(
                f"Publication failed for {proposal.proposed_name} "
                f"(proposal {proposal.proposal_id}): {error}",
            )
        except Exception:
            logger.warning(
                "publication: failure notification failed for proposal %s",
                proposal.proposal_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _staging_dir_for(self, proposal_id: str) -> Path:
        return self._config.memory_dir / "publications" / proposal_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
