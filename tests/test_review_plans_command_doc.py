"""Regression: review-plans.md ships robust, status-aware curl snippets.

`.claude/commands/review-plans.md` embeds two executable curl snippets an LLM
runs verbatim, and PR #3471 review surfaced two correctness hazards in them:

1. **Reject payload quoting (🐛).** The reject call built its JSON body inside a
   single-quoted ``-d '{"content_hash":"<hash>","reason":"<reason>"}'`` literal.
   A rejection reason is prose (naming symbols, up to 2000 chars) that almost
   always contains an apostrophe (``plan's``) or a double quote — a literal
   ``'`` terminates the shell string and a literal ``"`` emits invalid JSON the
   endpoint rejects with ``400``. Because the reason feeds
   ``learn_from_rejection()``, a garbled reason also corrupts rejection
   learning. The body must be built with a real JSON encoder and streamed via
   ``--data @-`` so arbitrary reason text is always safely encoded.

2. **Missing status capture (💡).** The bootstrap pending-queue fetch omitted
   any status capture, so ``curl`` (no ``-f``) exited 0 on 401/403/5xx and the
   documented auth-vs-unreachable branching (a stated safety property: 401/403 →
   ``error=dashboard_auth``; unreachable → JSONL fallback) could not be applied
   reliably. The fetch must capture ``%{http_code}`` so the branch can key on it.

These tests pin both fixes so the command doc cannot drift back to the fragile
single-quoted literal or the status-blind fetch.
"""

from __future__ import annotations

from pathlib import Path

_REVIEW_PLANS_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "review-plans.md"
)


def _line_containing(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


def test_reject_payload_uses_safe_json_encoder() -> None:
    doc = _REVIEW_PLANS_DOC.read_text(encoding="utf-8")

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
    doc = _REVIEW_PLANS_DOC.read_text(encoding="utf-8")
    assert "<<'REASON'" in doc, (
        "The rejection reason must be fed to the JSON encoder via a quoted "
        "heredoc (<<'REASON') so no shell expansion mangles it."
    )


def test_pending_fetch_captures_http_status() -> None:
    doc = _REVIEW_PLANS_DOC.read_text(encoding="utf-8")

    fetch = _line_containing(doc, "-o /tmp/pending.json")
    assert "-w '%{http_code}'" in fetch, (
        "The bootstrap pending-queue fetch must capture the HTTP status "
        "(`-w '%{http_code}'`) so the 401/403-vs-unreachable branching can key "
        f"on it — curl exits 0 on HTTP errors without it. Line:\n{fetch}"
    )
    # The branching must actually consume the captured status.
    assert "If `$CODE` is 401 or 403" in doc, (
        "Step 1 must branch on the captured `$CODE` (401/403 → dashboard_auth), "
        "not on curl's exit code alone."
    )
