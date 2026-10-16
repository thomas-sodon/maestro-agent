"""Regression: review-plan.md ships robust curl snippets and single-oldest scope.

`.claude/commands/review-plan.md` is the singular sibling of
`.claude/commands/review-plans.md`: it reviews the one *oldest* pending
`EvolutionPlan` rather than sweeping the whole queue. It embeds the same two
executable curl snippets an LLM runs verbatim, so it inherits the two
correctness properties `tests/test_review_plans_command_doc.py` pins:

1. **Reject payload quoting.** The reject body must be built with a real JSON
   encoder (``json.dumps``) and streamed via ``--data @…`` — never interpolated
   into a single-quoted ``-d '{"content_hash":…}'`` literal, since a rejection
   reason is prose that almost always contains an apostrophe or double quote
   (which would break the shell string / emit invalid JSON, and — because the
   reason feeds ``learn_from_rejection()`` — corrupt rejection learning).

2. **Status capture.** The bootstrap pending-queue fetch must capture
   ``%{http_code}`` so the documented 401/403-vs-unreachable branching can key
   on it — ``curl`` (no ``-f``) exits 0 on HTTP errors without it.

3. **Change-request verdict.** A plan that is sound but carries a fixable
   body defect gets a ``REQUEST_CHANGES`` verdict POSTed to the proposal-145
   feedback endpoint; that payload obeys the same encoder-safe rule as the
   reject payload, and the endpoint path must match the route the dashboard
   actually registers.

Plus the property that distinguishes this doc from its plural sibling: it must
review exactly the single oldest plan (naming selection by the minimum
``created_at``) and must **not** carry the plural command's multi-plan
``[<plan_id> …]`` positional selector.

These tests pin all four so the command doc cannot drift back to a fragile
single-quoted literal, a status-blind fetch, a two-verdict rubric, or a
whole-queue scope.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.command_docs import line_containing

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REVIEW_PLAN_DOC = (
    _REPO_ROOT
    / ".claude"
    / "commands"
    / "review-plan.md"
)


def test_reject_payload_uses_safe_json_encoder() -> None:
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")

    assert "-d '{\"content_hash\"" not in doc, (
        "The reject call must not interpolate the LLM-composed reason into a "
        "single-quoted `-d '{\"content_hash\":…}'` shell literal — an apostrophe "
        "or double quote in the reason breaks the shell string or emits invalid "
        "JSON. Build the body with a real encoder and stream it via `--data @-`."
    )
    assert 'json.dumps({"content_hash"' in doc, (
        "The reject payload must be built with a real JSON encoder "
        "(`json.dumps`) so apostrophes/quotes/newlines in the reason are safely "
        "escaped."
    )
    assert "--data @/tmp/reject-body.json" in doc, (
        "The encoded body must be streamed to curl via `--data @…` rather than "
        "interpolated into the command line."
    )


def test_reject_reason_passed_out_of_band_of_shell_parsing() -> None:
    """The reason must reach the encoder without further shell parsing.

    Passing the prose reason through a quoted heredoc (``<<'REASON'``) means no
    shell expansion or word-splitting touches it, so apostrophes, double quotes,
    and newlines survive verbatim into ``json.dumps``.
    """
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")
    assert "<<'REASON'" in doc, (
        "The rejection reason must be fed to the JSON encoder via a quoted "
        "heredoc (<<'REASON') so no shell expansion mangles it."
    )


def test_pending_fetch_captures_http_status() -> None:
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")

    fetch = line_containing(doc, "-o /tmp/pending.json")
    assert "-w '%{http_code}'" in fetch, (
        "The bootstrap pending-queue fetch must capture the HTTP status "
        "(`-w '%{http_code}'`) so the 401/403-vs-unreachable branching can key "
        f"on it — curl exits 0 on HTTP errors without it. Line:\n{fetch}"
    )
    # The branching must actually consume the captured status.
    assert "If `$CODE` is 401 or 403" in doc, (
        "The fetch step must branch on the captured `$CODE` (401/403 → "
        "dashboard_auth), not on curl's exit code alone."
    )


def test_reviews_single_oldest_plan_only() -> None:
    """The doc must scope to the one oldest plan, not the whole queue.

    The plural sibling reviews every pending plan and offers a
    ``<plan_id> [<plan_id> …]`` positional selector; this singular command
    fixes its scope to the plan with the minimum ``created_at`` and must not
    carry that multi-plan selector.
    """
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")

    assert "minimum `created_at`" in doc, (
        "The doc must name single-oldest selection by the minimum `created_at`, "
        "the property that distinguishes it from the whole-queue sibling."
    )
    assert "single oldest" in doc, (
        "The doc must state its scope is the single oldest pending plan, not the "
        "whole queue."
    )
    assert "[<plan_id>" not in doc, (
        "The singular command must not carry the plural sibling's multi-plan "
        "`[<plan_id> …]` positional selector — its scope is fixed to the oldest "
        "plan."
    )


def test_feedback_payload_uses_safe_json_encoder() -> None:
    """The change-request text is prose too, so it gets the reject rule."""
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")

    encoder = line_containing(doc, 'json.dumps({"content_hash": sys.argv[1], "text"')
    assert "sys.stdin.read()" in encoder, (
        "The feedback body must be built with `json.dumps` over the text read "
        f"from stdin. Line:\n{encoder}"
    )
    assert "--data @/tmp/feedback-body.json" in doc, (
        "The encoded feedback body must be streamed to curl via `--data @…`."
    )
    assert "<<'FEEDBACK'" in doc, (
        "The change-request text must reach the encoder through a quoted "
        "heredoc (<<'FEEDBACK') so no shell expansion mangles it."
    )
    assert "-d '{\"content_hash\"" not in doc


def test_request_changes_verdict_documented() -> None:
    doc = _REVIEW_PLAN_DOC.read_text(encoding="utf-8")

    assert "REQUEST_CHANGES" in doc, (
        "The doc must offer the REQUEST_CHANGES verdict for a sound plan with "
        "a fixable body defect."
    )
    assert "/api/plans/<plan_id>/feedback" in doc, (
        "The change request must POST to the dashboard feedback endpoint."
    )


def test_feedback_endpoint_matches_dashboard_route() -> None:
    """A renamed route must fail here, not leave the command posting to a 404."""
    routes = (_REPO_ROOT / "maestro" / "dashboard" / "routes.py").read_text(
        encoding="utf-8",
    )

    assert '"/api/plans/{plan_id}/feedback"' in routes
