"""Artifact review phase — classify completed tasks for publication.

Consumed by the ``review_artifacts`` loop phase (proposal 031). Runs every
``[artifacts] review_every`` iterations (plus the first iteration after
restart), selects up to ``review_batch_size`` newly-completed tasks whose
artifact directories are non-empty, and issues one bounded LLM call to
classify each as ``publish`` / ``improve`` / ``reuse`` / ``skip``.

Verdicts are persisted to ``artifact_reviews.jsonl``. In 031 the ``publish``
verdict is *inert* — no :class:`PublicationProposal` is emitted. Proposal
032 is what turns recorded ``publish`` rows into live approval surfaces.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import (
    ArtifactReview,
    DuplicateProposalError,
    Memory,
    PublicationProposal,
    TaskRecord,
)
from maestro.prompt_caps import MAX_TASK_GOAL_BYTES
from maestro.prompt_caps import TRUNCATED_SENTINEL as _TRUNCATED_SENTINEL
from maestro.prompt_caps import cap_utf8 as _cap_utf8
from maestro.prompts.artifact_review import ARTIFACT_REVIEW_PROMPT
from maestro.sdk import QueryOptions, json_complete, query, query_on_session
from maestro.secret_scan import has_hard_block, scan_contents, scrub_readme

if TYPE_CHECKING:
    from maestro.notifications.base import Notifier
    from maestro.providers.base import Session
    from maestro.vcs import CodeHost

logger = logging.getLogger("maestro")

_VALID_VERDICTS: frozenset[str] = frozenset({"publish", "improve", "reuse", "skip"})

# Mirrors the README floor stated in ``ARTIFACT_REVIEW_PROMPT``. The prompt
# requires ``readme_markdown`` to be at least this many characters for a
# ``publish`` verdict; ``_classify_and_persist`` downgrades any shorter or
# missing README so the persisted contract matches what the model was told.
_MIN_README_LENGTH = 600

# Suffix allowlist for bundle inclusion. Binary / non-text suffixes (and
# anything not listed here) are silently dropped so the bundle only ever
# contains reviewable text.
_ALLOWED_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".sh", ".rst", ".ini", ".cfg", ".html", ".css", ".js", ".ts",
})

# Context bloat caps (CLAUDE.md) for the task metadata rendered ahead of each
# bundle. Goals are persisted uncapped by ``Memory.record_task``, so without
# these a single pasted request could dwarf the byte-capped bundle. The
# summary cap mirrors the 500-char cap its producers already apply.
_MAX_GOAL_BYTES = MAX_TASK_GOAL_BYTES
_MAX_SUMMARY_BYTES = 500
_MAX_FILES_LINE_BYTES = 1024
# Per-block allowance for everything that is not bundle text; the whole
# review input is bounded by ``len(blocks) * (max_bundle + this)``.
_MAX_BLOCK_METADATA_BYTES = 4096


def _artifacts_non_empty(artifacts_dir: str | None) -> bool:
    """Return True iff *artifacts_dir* exists and contains at least one entry.

    Swallows OS errors (missing dir, permission denied, unreadable path)
    and returns False — a malformed ``artifacts_dir`` on one task must never
    crash the selection pass for the rest.
    """
    if not artifacts_dir:
        return False
    p = Path(artifacts_dir)
    try:
        if not p.is_dir():
            return False
        for _ in p.iterdir():
            return True
        return False
    except OSError:
        logger.warning(
            "artifact_review: skipping unreadable artifacts_dir %s",
            artifacts_dir,
        )
        return False


class ArtifactReviewer:
    """Classify artifact bundles of completed tasks and persist verdicts.

    Instantiated per-phase; no cross-phase state. The ``clock`` override is
    provided for tests that need to pin timestamps.
    """

    def __init__(
        self,
        memory: Memory,
        journal: Journal,
        config: Config,
        clock: Callable[[], datetime] | None = None,
        *,
        host: CodeHost | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.memory = memory
        self.journal = journal
        self.config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        # Optional wiring for publication-proposal emission (proposal 032).
        # When either is None the emission path short-circuits safely and the
        # reviewer behaves identically to the 031 inert-publish baseline.
        self._host = host
        self._notifier = notifier

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_candidates(self, batch_size: int) -> list[TaskRecord]:
        """Return up to *batch_size* tasks eligible for review, newest first.

        Eligibility: ``outcome`` ∈ {``success``, ``partial``},
        ``artifacts_dir`` set and non-empty on disk, and the task id has
        not already been reviewed.
        """
        if batch_size <= 0:
            return []
        reviewed = self.memory.get_reviewed_task_ids()
        tasks = self.memory._load_all_tasks()
        eligible: list[TaskRecord] = []
        for t in tasks:
            if t.outcome not in ("success", "partial"):
                continue
            if t.task_id in reviewed:
                continue
            if not _artifacts_non_empty(t.artifacts_dir):
                continue
            eligible.append(t)
        eligible.sort(key=lambda t: t.timestamp or "", reverse=True)
        return eligible[:batch_size]

    # ------------------------------------------------------------------
    # Bundle construction
    # ------------------------------------------------------------------

    def _read_artifact_bundle(self, task: TaskRecord) -> tuple[str, list[str]]:
        """Read a bounded, text-only bundle from *task*'s artifact directory.

        Returns ``(prompt_text, file_names_included)`` where *prompt_text*
        is a block of ``===== <relative-name> =====\\n<body>`` sections
        ready to drop into the review prompt.

        Caps:
          - per-file head-truncate at ``review_max_file_bytes``
          - max ``review_max_files_per_task`` files included
          - running total hard-capped at ``review_max_bundle_bytes``

        Containment: only files whose *resolved* target lies under the
        resolved artifact root are read, and the bytes come from that
        validated path — mirroring ``Publisher._stage`` — so a symlink
        cannot pull out-of-tree content into the priced prompt (#3989).
        """
        if not task.artifacts_dir:
            return "", []
        base = Path(task.artifacts_dir)
        if not base.is_dir():
            return "", []
        try:
            root = base.resolve(strict=True)
        except (OSError, RuntimeError):
            return "", []

        max_file = self.config.artifact_review_max_file_bytes
        max_count = self.config.artifact_review_max_files_per_task
        max_bundle = self.config.artifact_review_max_bundle_bytes

        # Deterministic order so the prompt is stable across runs.
        try:
            entries = sorted(base.rglob("*"))
        except OSError:
            logger.warning(
                "artifact_review: failed to walk %s", task.artifacts_dir,
            )
            return "", []

        parts: list[str] = []
        names_included: list[str] = []
        running_size = 0
        for entry in entries:
            if len(names_included) >= max_count:
                break
            try:
                resolved = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            rel_name = str(entry.relative_to(base))
            if not resolved.is_relative_to(root):
                logger.debug(
                    "artifact_review: skipping %s — resolves outside the "
                    "artifact root", rel_name,
                )
                continue
            try:
                if not resolved.is_file():
                    continue
            except OSError:
                continue
            if entry.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            if resolved.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(text.encode("utf-8")) > max_file:
                text = (
                    text.encode("utf-8")[:max_file]
                    .decode("utf-8", errors="ignore")
                    + _TRUNCATED_SENTINEL
                )
            header = f"===== {rel_name} =====\n"
            section = header + text + "\n"
            section_bytes = len(section.encode("utf-8"))
            if running_size + section_bytes > max_bundle:
                break
            parts.append(section)
            names_included.append(rel_name)
            running_size += section_bytes
        return "".join(parts), names_included

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    def _format_task_block(
        self, task: TaskRecord, bundle_text: str, names: list[str],
    ) -> str:
        """Render one task into the prompt's task block.

        Goal, summary, and file list are capped, and the bundle is trimmed
        so the block never exceeds :meth:`_block_budget_bytes`. The task id
        is never truncated — the model echoes it back for matching.
        """
        goal = _cap_utf8((task.goal or "").strip(), _MAX_GOAL_BYTES)
        summary = _cap_utf8(
            (task.result_summary or "").strip(), _MAX_SUMMARY_BYTES,
        )
        header = (
            f"--- Task {task.task_id} ---\n"
            f"Goal: {goal}\n"
            f"Outcome: {task.outcome}\n"
        )
        if summary:
            header += f"Summary: {summary}\n"
        if names:
            files = _cap_utf8(", ".join(names), _MAX_FILES_LINE_BYTES)
            header += f"Files: {files}\n"
        else:
            header += "Files: <none>\n"
        header += "\n"
        room = self._block_budget_bytes() - len(header.encode("utf-8")) - 1
        bundle_text = _cap_utf8(bundle_text, max(0, room))
        return header + bundle_text + "\n"

    def _block_budget_bytes(self) -> int:
        """Total UTF-8 byte budget for one rendered task block."""
        return (
            self.config.artifact_review_max_bundle_bytes
            + _MAX_BLOCK_METADATA_BYTES
        )

    async def review(
        self,
        candidates: list[TaskRecord],
        *,
        budget_throttle_active: bool = False,
        session: Session | None = None,
    ) -> list[ArtifactReview]:
        """Classify each candidate and persist the verdict.

        Returns the list of persisted reviews (may be shorter than
        *candidates* when the LLM drops or malforms entries). Silently
        returns ``[]`` under budget throttle or when no candidates exist.

        Backfill is a separate entry point — see :meth:`backfill`.

        When *session* is provided, the SDK call is dispatched on the
        session so the identity prompt is cached and the
        ``review_artifacts`` phase has transcript continuity. The
        ``query()`` fallback is retained for tests and the
        ``enable_session_reuse=False`` configuration path.
        """
        if budget_throttle_active or not candidates:
            return []

        bundles: list[tuple[TaskRecord, str, list[str]]] = []
        task_blocks: list[str] = []
        for task in candidates:
            bundle_text, names = self._read_artifact_bundle(task)
            bundles.append((task, bundle_text, names))
            task_blocks.append(self._format_task_block(task, bundle_text, names))

        # Each block is trimmed to ``_block_budget_bytes``, so the joined
        # input is bounded by ``len(task_blocks) * (budget + 1)``.
        prompt = ARTIFACT_REVIEW_PROMPT.format(
            bundle_text="\n".join(task_blocks),
        )

        try:
            if session is not None:
                result = await query_on_session(
                    session, prompt, caller="artifact_review",
                    complete_on=json_complete,
                )
            else:
                result = await query(
                    prompt=prompt,
                    options=QueryOptions(
                        effort="medium",
                        caller="artifact_review",
                    ),
                    complete_on=json_complete,
                )
        except Exception:
            logger.warning(
                "artifact_review: LLM call failed, skipping cycle",
                exc_info=True,
            )
            return []

        try:
            entries = result.parse_json()
        except Exception:
            logger.warning(
                "artifact_review: failed to parse LLM response as JSON",
                exc_info=True,
            )
            return []
        if not isinstance(entries, list):
            logger.warning(
                "artifact_review: LLM returned non-list payload (%s), skipping",
                type(entries).__name__,
            )
            return []

        persisted: list[ArtifactReview] = []
        # Index bundles by task_id so the model is allowed to reorder entries.
        bundles_by_id: dict[str, tuple[TaskRecord, str, list[str]]] = {
            b[0].task_id: b for b in bundles
        }
        seen: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                logger.warning(
                    "artifact_review: skipping non-dict entry (%s)",
                    type(entry).__name__,
                )
                continue
            task_id = entry.get("task_id")
            if not isinstance(task_id, str) or task_id not in bundles_by_id:
                logger.warning(
                    "artifact_review: skipping entry with unknown task_id %r",
                    task_id,
                )
                continue
            if task_id in seen:
                logger.warning(
                    "artifact_review: skipping duplicate entry for task_id %r in single response",
                    task_id,
                )
                continue
            seen.add(task_id)
            task, bundle_text, names = bundles_by_id[task_id]
            review = self._classify_and_persist(task, bundle_text, names, entry)
            persisted.append(review)
            if (
                review.verdict == "publish"
                and self._publication_emission_enabled()
            ):
                await self._emit_publication_if_eligible(task, review)
        return persisted

    # ------------------------------------------------------------------
    # Per-entry handling
    # ------------------------------------------------------------------

    def _classify_and_persist(
        self,
        task: TaskRecord,
        bundle_text: str,
        names: list[str],
        entry: dict[str, Any],
    ) -> ArtifactReview:
        """Validate one LLM entry, persist the review, and side-effect."""
        raw_verdict = entry.get("verdict")
        if not isinstance(raw_verdict, str) or raw_verdict not in _VALID_VERDICTS:
            logger.warning(
                "artifact_review: task %s has invalid verdict %r, defaulting to skip",
                task.task_id, raw_verdict,
            )
            verdict = "skip"
        else:
            verdict = raw_verdict

        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            title = (task.goal or task.task_id)[:80]
        else:
            title = title.strip()[:200]

        reasoning = entry.get("reasoning")
        if not isinstance(reasoning, str):
            reasoning = ""
        reasoning = reasoning.strip()

        proposed_name: str | None = None
        proposed_description: str | None = None
        proposed_readme: str | None = None
        if verdict == "publish":
            raw_name = entry.get("proposed_name")
            raw_desc = entry.get("proposed_description")
            raw_readme = entry.get("readme_markdown")
            if isinstance(raw_name, str) and raw_name.strip():
                proposed_name = raw_name.strip()[:40]
            if isinstance(raw_desc, str) and raw_desc.strip():
                proposed_description = raw_desc.strip()[:120]
            if isinstance(raw_readme, str) and raw_readme.strip():
                proposed_readme = raw_readme.strip()

            # Pre-classification hard-block scan. Runs over the truncated
            # bundle; the definitive scan lives in the 033 publish path.
            if has_hard_block(bundle_text):
                scan = scan_contents(bundle_text)
                # Findings look like "kind: preview"; keep only the kind.
                first_kind = scan.hard_block_findings[0].split(":", 1)[0]
                verdict = "improve"
                suffix = f" [scrub_blocked:{first_kind}]"
                reasoning = (reasoning + suffix).strip() if reasoning else suffix.strip()
                proposed_name = None
                proposed_description = None
                proposed_readme = None

            # ARTIFACT_REVIEW_PROMPT promises ``readme_markdown`` and
            # ``proposed_name`` are required for ``publish`` (the README
            # must also clear ``_MIN_README_LENGTH``). The hard-block branch
            # above already nulls both on downgrade, so these follow-ups
            # only fire for entries that survived the hard-block check —
            # a nameless ``publish`` row could never be emitted and would
            # hold a backfill slot forever.
            if verdict == "publish" and (
                proposed_readme is None or len(proposed_readme) < _MIN_README_LENGTH
            ):
                verdict = "improve"
                suffix = " [readme_missing]"
                reasoning = (reasoning + suffix).strip() if reasoning else suffix.strip()
                proposed_name = None
                proposed_description = None
                proposed_readme = None
            if verdict == "publish" and proposed_name is None:
                verdict = "improve"
                suffix = " [name_missing]"
                reasoning = (reasoning + suffix).strip() if reasoning else suffix.strip()
                proposed_description = None
                proposed_readme = None

        review = ArtifactReview(
            review_id=uuid.uuid4().hex,
            task_id=task.task_id,
            verdict=verdict,  # type: ignore[arg-type]
            title=title,
            reasoning=reasoning,
            bundle_file_names=list(names),
            proposed_name=proposed_name,
            proposed_description=proposed_description,
            timestamp=self._clock().isoformat(),
            proposed_readme=proposed_readme,
        )
        self.memory.write_artifact_review(review)
        self._emit_side_effects(task, review)
        return review

    def _emit_side_effects(self, task: TaskRecord, review: ArtifactReview) -> None:
        """Journal / log the post-persist side effects for a verdict.

        ``improve`` / ``reuse`` → ``realization`` journal event (an
        operational record, never the inner-thought journal). ``publish`` →
        INFO log only (032 owns proposal emission). ``skip`` → persist only.
        """
        if review.verdict in ("improve", "reuse"):
            content = (
                f"Artifact review ({review.verdict}) for task {task.task_id}: "
                f"{review.title}. {review.reasoning}"
            ).strip()
            self.journal.record_event(
                content=content,
                entry_type="realization",
                triggered_by="artifact_review",
            )
        elif review.verdict == "publish":
            logger.info(
                "artifact_review: publish verdict recorded for task %s; "
                "review_id=%s",
                task.task_id,
                review.review_id,
            )

    # ------------------------------------------------------------------
    # Publication-proposal emission (proposal 032)
    # ------------------------------------------------------------------

    def _publication_emission_enabled(self) -> bool:
        """Return True iff the publication approval surface is fully wired.

        All three ingredients are required: the config flag, a code host
        (for owner resolution + repo-exists pre-checks), and a notifier
        (so Thomas sees the approval prompt). Any missing piece
        short-circuits the emission path silently — legacy tests that
        construct an ``ArtifactReviewer`` without host/notifier keep the
        031 inert-publish behaviour.
        """
        return (
            self.config.publication_enabled
            and self._host is not None
            and self._notifier is not None
        )

    async def _emit_publication_if_eligible(
        self,
        task: TaskRecord,
        review: ArtifactReview,
        *,
        suppress_journal: bool = False,
    ) -> None:
        """Emit a :class:`PublicationProposal` for a freshly-persisted publish row.

        Three pre-checks run in order:

        1. **Missing name** — a ``publish`` row with no usable
           ``proposed_name`` (legacy or hand-edited) is demoted terminally
           to ``improve`` with ``[name_missing]`` before any host call.
        2. **README scrub** — hard-block findings in the drafted README
           downgrade the verdict to ``improve`` with a
           ``[scrub_blocked:<kind>]`` reasoning suffix. This is a second
           line of defence after the 031 bundle-level hard-block scan —
           the drafted README may paraphrase content the bundle scan
           didn't match verbatim.
        3. **Name collision** — if the host reports the proposed repo
           name already exists under the resolved owner, demote to
           ``improve`` with ``[name_taken:<name>]``.

        :class:`DuplicateProposalError` is the one tolerated failure —
        a concurrent phase or mid-emission retry already persisted the
        proposal, so the call is idempotent. Every other failure
        (persist OSError, notifier down, journal write failing)
        propagates so the loop's iteration handler can classify or
        crash it. The underlying review is durably persisted before
        this method runs, but backfill recovery depends on *when* a
        failure propagates: ``_maybe_backfill_proposals`` re-emits only
        rows whose ``review_id`` has no :class:`PublicationProposal`
        yet. A failure at or before
        :meth:`Memory.write_publication_proposal` leaves the row
        proposal-less and thus eligible for the next backfill pass; a
        failure after the proposal persists (notifier down, journal
        write failing) leaves the proposal in place — visible on the
        dashboard approval surface — but backfill's ``seen_review_ids``
        filter then skips the row, so the missed Telegram notification
        / journal milestone is not re-emitted.
        """
        # Quiet the type checker — ``_publication_emission_enabled``
        # already guaranteed these are non-None.
        assert self._host is not None
        assert self._notifier is not None

        # A nameless ``publish`` row (legacy or hand-edited — review()
        # refuses to write one) can never be emitted. Demote it terminally,
        # ahead of any host call, so it stops holding a backfill slot.
        proposed_name = (review.proposed_name or "").strip()
        if not proposed_name:
            self.memory.update_artifact_review_verdict(
                review.review_id, "improve",
                f"{review.reasoning} [name_missing]".strip(),
            )
            logger.info(
                "artifact_review: publish downgraded — missing "
                "proposed_name for task %s",
                task.task_id,
            )
            return

        readme = review.proposed_readme or ""
        scan = scrub_readme(readme)
        if scan.hard_block_findings:
            first_kind = scan.hard_block_findings[0].split(":", 1)[0]
            demoted_reasoning = (
                f"{review.reasoning} [scrub_blocked:{first_kind}]"
            ).strip()
            self.memory.update_artifact_review_verdict(
                review.review_id, "improve", demoted_reasoning,
            )
            logger.info(
                "artifact_review: publish downgraded — README hard-block "
                "(%s) for task %s",
                first_kind, task.task_id,
            )
            return

        # Resolve owner (may raise on auth/network failure). A missing
        # owner is fatal for the publication pipeline but not for the
        # review — log and skip.
        try:
            owner = (
                self.config.publication_owner
                or self._host.resolve_owner()
            )
        except Exception:
            logger.warning(
                "artifact_review: resolve_owner failed for task %s; "
                "skipping publication emission",
                task.task_id, exc_info=True,
            )
            return
        if not owner:
            logger.warning(
                "artifact_review: no publication owner configured or "
                "resolvable; skipping emission for task %s",
                task.task_id,
            )
            return

        # Name/description secret guard (issue #1417). The LLM-proposed
        # repo name and description flow into ``gh repo create`` and are
        # indexed publicly. The repo name is a kebab slug (≤40 chars) that
        # cannot carry a placeholder redaction, so ANY finding in it — a
        # hard block or a narrative-redaction target — demotes to
        # ``improve`` like the README branch above. The description is
        # free text: a hard block demotes, but narrative-redaction targets
        # are scrubbed in place so the scrubbed value is what reaches
        # public metadata.
        name_scan = scan_contents(proposed_name)
        desc_scan = scan_contents(review.proposed_description or "")
        name_findings = name_scan.hard_block_findings or name_scan.redact_findings
        if name_findings or desc_scan.hard_block_findings:
            blocking = name_findings or desc_scan.hard_block_findings
            first_kind = blocking[0].split(": ", 1)[0]
            demoted_reasoning = (
                f"{review.reasoning} [scrub_blocked:{first_kind}]"
            ).strip()
            self.memory.update_artifact_review_verdict(
                review.review_id, "improve", demoted_reasoning,
            )
            logger.info(
                "artifact_review: publish downgraded — name/description "
                "scrub-block (%s) for task %s",
                first_kind, task.task_id,
            )
            return

        try:
            collision = self._host.repo_exists(owner, proposed_name)
        except Exception:
            logger.warning(
                "artifact_review: repo_exists check failed for %s/%s; "
                "skipping emission",
                owner, proposed_name, exc_info=True,
            )
            return
        if collision:
            demoted_reasoning = (
                f"{review.reasoning} [name_taken:{proposed_name}]"
            ).strip()
            self.memory.update_artifact_review_verdict(
                review.review_id, "improve", demoted_reasoning,
            )
            logger.info(
                "artifact_review: publish downgraded — repo %s/%s "
                "already exists",
                owner, proposed_name,
            )
            return

        proposal = PublicationProposal(
            proposal_id=uuid.uuid4().hex[:12],
            task_id=task.task_id,
            review_id=review.review_id,
            proposed_name=proposed_name,
            description=desc_scan.scrubbed_content,
            proposed_readme=scan.scrubbed_content,
            publishable_files=list(review.bundle_file_names),
            scrub_findings=list(scan.redact_findings)
            + list(desc_scan.redact_findings),
            created_at=self._clock().isoformat(),
        )
        try:
            self.memory.write_publication_proposal(proposal)
        except DuplicateProposalError:
            # Two concurrent phases (shouldn't happen under single-loop
            # semantics) or a retry after a mid-emission crash.
            logger.warning(
                "artifact_review: duplicate proposal_id %s — skipping",
                proposal.proposal_id,
            )
            return

        body = self._format_publication_notification(task, proposal, review)
        await self._notifier.notify_publication_for_review(
            proposal.proposal_id, body,
        )

        if not suppress_journal:
            self.journal.record_event(
                content=(
                    f"Publication proposal {proposal.proposal_id} "
                    f"emitted for task {task.task_id}: {proposed_name}"
                ),
                entry_type="milestone",
                triggered_by="publication_proposal",
            )

    @staticmethod
    def _format_publication_notification(
        task: TaskRecord,
        proposal: PublicationProposal,
        review: ArtifactReview,
    ) -> str:
        """Build the human-readable Telegram body for a new proposal.

        ``task.goal`` is user-authored prompt text that has never been
        scrubbed — bundles and READMEs are scrubbed elsewhere, but the
        originating goal is not. Run it through ``scan_contents`` so
        soft-redact patterns (paths, emails, etc.) are masked and a
        hard-block hit replaces the line with a placeholder rather than
        leaking the credential into Telegram.
        """
        lines = [
            "Publication proposal ready for review",
            f"Name: {proposal.proposed_name}",
        ]
        if proposal.description:
            lines.append(f"Description: {proposal.description}")
        lines.append(f"Source task: {task.task_id}")
        if task.goal:
            goal_scan = scan_contents(task.goal)
            if goal_scan.hard_block_findings:
                first_kind = goal_scan.hard_block_findings[0].split(
                    ":", 1,
                )[0]
                lines.append(
                    f"Goal: (redacted: contains {first_kind})",
                )
            else:
                cleaned = goal_scan.scrubbed_content
                goal = cleaned[:160] + ("…" if len(cleaned) > 160 else "")
                lines.append(f"Goal: {goal}")
        lines.append(f"Files: {len(proposal.publishable_files)}")
        if proposal.scrub_findings:
            kinds = sorted({s.split(":", 1)[0] for s in proposal.scrub_findings})
            lines.append(f"Scrubbed: {', '.join(kinds)}")
        if review.reasoning:
            reasoning = review.reasoning[:240] + (
                "…" if len(review.reasoning) > 240 else ""
            )
            lines.append(f"Rationale: {reasoning}")
        return "\n".join(lines)

    async def backfill(self) -> None:
        """Emit proposals for historical ``publish`` rows with no proposal.

        Entry point exposed on the reviewer so the phase dispatch can run
        backfill independently of live candidate classification — the
        backfill pass must keep firing even on ticks with zero live
        candidates so flipping ``[publication] enabled`` retroactively is
        observable. Silently returns when publication emission is not
        fully wired (flag off, no host, or no notifier).
        """
        if not self._publication_emission_enabled():
            return
        await self._maybe_backfill_proposals()

    async def _maybe_backfill_proposals(self) -> None:
        """Emit proposals for historical ``publish`` rows with no proposal.

        A ``publish`` verdict is eligible for backfill when:

        * the row's ``verdict`` is still ``publish`` (not demoted),
        * no :class:`PublicationProposal` with the same ``review_id``
          exists (match is by review id, not by proposal status — a
          rejected or approved proposal still counts as "seen"), and
        * emitting it does not exceed the per-tick cap
          (``artifact_review_batch_size``).

        The per-tick cap prevents a backlog of N historical publish rows
        from producing N notifications + N journal milestones in one
        phase. Any excess rolls over to the next ``review_artifacts``
        tick. Per-proposal journal milestones are skipped here — one
        ``milestone`` summary covers the whole backfill batch so the
        journal doesn't balloon.
        """
        try:
            all_reviews = self.memory._load_all_reviews()
            seen_review_ids = {
                p.review_id
                for p in self.memory.get_publication_proposals()
                if p.review_id
            }
        except Exception:
            logger.warning(
                "artifact_review: backfill scan failed to load state",
                exc_info=True,
            )
            return

        pending = [
            r for r in all_reviews
            if r.verdict == "publish" and r.review_id not in seen_review_ids
        ]
        if not pending:
            return

        cap = max(1, self.config.artifact_review_batch_size)
        to_process = pending[:cap]

        # Map review_id → TaskRecord for the pending rows only. Loading
        # the full task history once is cheaper than per-review lookups.
        task_by_id: dict[str, TaskRecord] = {}
        try:
            for t in self.memory._load_all_tasks():
                task_by_id[t.task_id] = t
        except Exception:
            logger.warning(
                "artifact_review: backfill failed to load task history",
                exc_info=True,
            )
            return

        # Count emitted proposals so the summary milestone can cite an
        # accurate figure (demotions inside ``_emit_publication_if_eligible``
        # do not produce proposals; only true emissions do).
        before = len(seen_review_ids)
        for review in to_process:
            task = task_by_id.get(review.task_id)
            if task is None:
                logger.warning(
                    "artifact_review: backfill skipping review %s — "
                    "source task %s no longer in history",
                    review.review_id, review.task_id,
                )
                continue
            # Per-proposal milestone is suppressed — one summary entry
            # is written for the whole backfill batch below so N
            # historical rows produce N Telegram pings + 1 milestone
            # instead of N milestones.
            await self._emit_publication_if_eligible(
                task, review, suppress_journal=True,
            )

        # Re-read the proposals file to count actual new emissions
        # (demotions inside the emission helper do not produce proposals).
        try:
            after = len({
                p.review_id
                for p in self.memory.get_publication_proposals()
                if p.review_id
            })
        except Exception:
            logger.warning(
                "artifact_review: backfill summary count failed",
                exc_info=True,
            )
            return

        emitted = max(0, after - before)
        if emitted == 0:
            return

        overflow = max(0, len(pending) - len(to_process))
        summary = (
            f"Publication backfill emitted {emitted} proposal(s) "
            f"from historical publish verdicts"
        )
        if overflow:
            summary += f"; {overflow} more queued for future ticks"
        try:
            self.journal.record_event(
                content=summary,
                entry_type="milestone",
                triggered_by="publication_backfill",
            )
        except Exception:
            logger.warning(
                "artifact_review: failed to journal backfill summary",
                exc_info=True,
            )
