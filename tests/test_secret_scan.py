"""Unit tests for :mod:`maestro.secret_scan`.

These are fast, pure-function tests — no I/O outside ``mem_fs``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.secret_scan import (
    HARD_BLOCK_PATTERNS,
    REDACT_PATTERNS,
    ScanResult,
    _truncate_preview,
    has_hard_block,
    scan_contents,
    scrub_file,
    scrub_readme,
)

# ---------------------------------------------------------------------------
# Hard-block patterns — one positive + one false-positive guard per family
# ---------------------------------------------------------------------------


def test_hard_block_anthropic_key_positive() -> None:
    text = "config: sk-ant-api03-AAAAAAAAAAAAAAAAAAAA more text"
    result = scan_contents(text)
    assert any(f.startswith("anthropic_key:") for f in result.hard_block_findings)


def test_hard_block_anthropic_key_false_positive_word_glued() -> None:
    """``sk-ant-`` welded to a preceding word character must not match."""
    text = "foosk-ant-api03-" + "X" * 40
    result = scan_contents(text)
    assert not any(
        f.startswith("anthropic_key:") for f in result.hard_block_findings
    )


def test_hard_block_openai_key_positive() -> None:
    # 40 alphanumerics after sk- (no ant- prefix)
    text = "OPENAI_KEY=sk-" + "A" * 40
    result = scan_contents(text)
    assert any(f.startswith("openai_key:") for f in result.hard_block_findings)


def test_hard_block_openai_no_overlap_with_anthropic() -> None:
    """The openai negative lookahead must prevent claiming an anthropic key."""
    text = "key=sk-ant-api03-" + "X" * 40
    result = scan_contents(text)
    kinds = [f.split(":", 1)[0] for f in result.hard_block_findings]
    assert "anthropic_key" in kinds
    assert "openai_key" not in kinds


def test_hard_block_openai_false_positive_short() -> None:
    """``sk-test`` (or any sub-32-char ``sk-...``) must not match openai."""
    result = scan_contents("placeholder = sk-test")
    assert not result.hard_block_findings


def test_hard_block_github_classic_positive() -> None:
    text = "GH_TOKEN=ghp_" + "B" * 40
    result = scan_contents(text)
    assert any(
        f.startswith("github_token_classic:") for f in result.hard_block_findings
    )


def test_hard_block_github_classic_false_positive_short() -> None:
    """``ghp_`` shorter than 36 chars must not match."""
    result = scan_contents("not a token: ghp_short")
    assert not result.hard_block_findings


def test_hard_block_github_pat_fine_grained_positive() -> None:
    """Modern fine-grained PAT (``github_pat_<82+ chars>``)."""
    text = "PAT=github_pat_" + "C" * 90
    result = scan_contents(text)
    assert any(
        f.startswith("github_pat_fine_grained:")
        for f in result.hard_block_findings
    )


def test_hard_block_github_pat_false_positive_too_short() -> None:
    """``github_pat_`` shorter than 82 chars must not match."""
    text = "fake = github_pat_" + "C" * 50
    result = scan_contents(text)
    assert not any(
        f.startswith("github_pat_fine_grained:")
        for f in result.hard_block_findings
    )


def test_hard_block_github_pat_false_positive_overlong() -> None:
    """``github_pat_`` longer than 255 chars must not match — the ``\\b``
    anchor cannot fall inside an unbroken run of word characters, so the
    upper-bound ceiling rejects pathologically long bodies the way the
    classic-token entry already does."""
    text = "fake = github_pat_" + "C" * 300
    result = scan_contents(text)
    assert not any(
        f.startswith("github_pat_fine_grained:")
        for f in result.hard_block_findings
    )


def test_hard_block_aws_positive() -> None:
    text = "aws_access_key_id = AKIAABCDEFGHIJKLMNOP"
    result = scan_contents(text)
    assert any(f.startswith("aws_access_key:") for f in result.hard_block_findings)


def test_hard_block_aws_false_positive_lowercase() -> None:
    """AKIA followed by 16 chars but with lowercase letters must not match."""
    result = scan_contents("AKIAabcdefghijklmnop")
    assert not any(
        f.startswith("aws_access_key:") for f in result.hard_block_findings
    )


def test_hard_block_private_key_positive_rsa() -> None:
    result = scan_contents("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n")
    assert any(f.startswith("private_key:") for f in result.hard_block_findings)


def test_hard_block_private_key_positive_openssh() -> None:
    result = scan_contents("-----BEGIN OPENSSH PRIVATE KEY-----\n...")
    assert any(f.startswith("private_key:") for f in result.hard_block_findings)


def test_hard_block_private_key_positive_pgp() -> None:
    """Real OpenPGP armor (RFC 4880) uses the ``PRIVATE KEY BLOCK`` terminus."""
    result = scan_contents("-----BEGIN PGP PRIVATE KEY BLOCK-----\n...")
    assert any(f.startswith("private_key:") for f in result.hard_block_findings)


def test_hard_block_private_key_positive_pkcs8() -> None:
    """The generic PKCS#8 container header carries usable key material."""
    text = "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n"
    result = scan_contents(text)
    assert any(f.startswith("private_key:") for f in result.hard_block_findings)
    assert has_hard_block(text) is True


def test_hard_block_private_key_positive_pkcs8_encrypted() -> None:
    """The encrypted PKCS#8 container header is blocked too."""
    text = (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIF...\n"
        "-----END ENCRYPTED PRIVATE KEY-----\n"
    )
    result = scan_contents(text)
    assert any(f.startswith("private_key:") for f in result.hard_block_findings)
    assert has_hard_block(text) is True


@pytest.mark.parametrize(
    "label",
    [
        "OPENSSH PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
    ],
)
def test_private_key_pattern_covers_every_pem_family(label: str) -> None:
    """Every PEM label family the policy claims must match at the owner."""
    pattern = dict(HARD_BLOCK_PATTERNS)["private_key"]
    assert pattern.search(f"-----BEGIN {label}-----\n...") is not None


def test_hard_block_private_key_pgp_without_block_does_not_match() -> None:
    """``PGP PRIVATE KEY`` without the ``BLOCK`` suffix is not a real format."""
    result = scan_contents("-----BEGIN PGP PRIVATE KEY-----\n...")
    assert not any(
        f.startswith("private_key:") for f in result.hard_block_findings
    )


def test_hard_block_private_key_false_positive_public() -> None:
    """A public key header must not trigger the private-key matcher."""
    result = scan_contents("-----BEGIN RSA PUBLIC KEY-----\n...")
    assert not any(
        f.startswith("private_key:") for f in result.hard_block_findings
    )
    for header in ("PUBLIC KEY", "ENCRYPTED PUBLIC KEY"):
        assert has_hard_block(f"-----BEGIN {header}-----\n...") is False


def test_hard_block_keyword_base64_positive() -> None:
    """Keyword followed within 32 chars by 80+ chars of base64."""
    text = 'api_key: "' + "A" * 100 + '"'
    result = scan_contents(text)
    assert any(f.startswith("keyword_b64:") for f in result.hard_block_findings)


def test_hard_block_keyword_base64_false_positive_benign_b64() -> None:
    """A 200-char base64 run with no nearby credential keyword must not match."""
    text = "Some payload: " + ("A" * 200)
    result = scan_contents(text)
    assert not any(
        f.startswith("keyword_b64:") for f in result.hard_block_findings
    )


# ---------------------------------------------------------------------------
# Inline redactions
# ---------------------------------------------------------------------------


def test_redact_paths_home_workspace_tmp() -> None:
    text = (
        "Logs at /home/operator/projects/devA/maestro/foo.log\n"
        "Build dir /workspace/build\n"
        "Temp file /tmp/scratch.txt\n"
    )
    result = scan_contents(text)
    assert "/home/operator/" not in result.scrubbed_content
    assert "/workspace/" not in result.scrubbed_content
    assert "/tmp/" not in result.scrubbed_content
    assert result.scrubbed_content.count("<redacted-path>") == 3
    assert any(f.startswith("redacted-path:") for f in result.redact_findings)


def test_redact_path_preserves_trailing_punctuation() -> None:
    """Trailing prose punctuation must not be swallowed into the redaction.

    Regression for #1442: the path patterns previously used ``\\S+``, which
    consumes punctuation. On prose like ``See /tmp/foo, which ...`` the
    match extended to ``/tmp/foo,`` and the substitution lost the comma,
    corrupting rendered output for artifacts surfaced as public READMEs.
    """
    cases = [
        (
            "See /tmp/foo, which contains data.",
            "See <redacted-path>, which contains data.",
        ),
        (
            "Logs under /home/operator/devA/maestro/log.txt; rotate weekly.",
            "Logs under <redacted-path>; rotate weekly.",
        ),
        (
            "Open (/tmp/foo) for details.",
            "Open (<redacted-path>) for details.",
        ),
    ]
    for text, expected in cases:
        result = scan_contents(text)
        assert result.scrubbed_content == expected


def test_redact_path_preserves_prose_terminators() -> None:
    """Less common prose/markdown terminators must also be preserved.

    Follow-up to the trailing-punctuation fix: `!`, `?`, `>`, and backtick
    are valid prose/markdown terminators (`See /tmp/foo!`, `` `/tmp/foo` ``)
    and must not be consumed into the path match and dropped.
    """
    assert scan_contents("See /tmp/foo!").scrubbed_content == "See <redacted-path>!"
    assert (
        scan_contents("Where is /tmp/foo?").scrubbed_content
        == "Where is <redacted-path>?"
    )
    assert (
        scan_contents("Path: `/tmp/foo`").scrubbed_content
        == "Path: `<redacted-path>`"
    )
    assert (
        scan_contents("Quoted: >/tmp/foo>").scrubbed_content
        == "Quoted: ><redacted-path>>"
    )


def test_redact_emails() -> None:
    text = (
        "Owner: operator@example.com\n"
        "Bot: agent+loop@mail.example.org\n"
    )
    result = scan_contents(text)
    assert "operator@example.com" not in result.scrubbed_content
    assert "agent+loop@mail.example.org" not in result.scrubbed_content
    kinds = {f.rsplit(":", 1)[0] for f in result.redact_findings}
    assert "redacted-email" in kinds


def test_redact_narrative_phrases() -> None:
    text = (
        "Maestro is a self-evolving, self-aware autonomous AI agent.\n"
        "It also self reflects and self-modifying."
    )
    result = scan_contents(text)
    lowered = result.scrubbed_content.lower()
    assert "maestro" not in lowered
    assert "self-evolving" not in lowered
    assert "self-aware" not in lowered
    assert "autonomous" not in lowered
    # Findings are formatted as "kind: count" — kind itself contains a
    # colon for the narrative redactions, so split on the *last* colon.
    kinds = {f.rsplit(":", 1)[0] for f in result.redact_findings}
    assert "narrative_redactions:name_maestro" in kinds
    assert "narrative_redactions:self_phrase" in kinds
    assert "narrative_redactions:autonomous_agent" in kinds


def test_self_phrase_fully_redacts_self_awareness() -> None:
    """``self-awareness`` must be fully replaced, not leave a trailing ``ness``.

    Regression: the alternation previously listed ``aware`` before
    ``awareness``. Python's ``re`` tries branches left-to-right, so on
    input ``self-awareness`` it matched ``self-aware`` and left ``ness``
    unconsumed, producing ``<redacted>ness`` — defeating the scrubber for
    a canonical Maestro term (it's also the name of a stage).
    """
    result = scan_contents("self-awareness is a stage")
    assert result.scrubbed_content == "<redacted> is a stage"


def test_redact_findings_aggregate_count() -> None:
    """Multiple matches of the same kind are reported as a single ``kind: count``."""
    text = "/tmp/a /tmp/b /tmp/c"
    result = scan_contents(text)
    path_findings = [f for f in result.redact_findings if f.startswith("redacted-path:")]
    assert len(path_findings) == 1
    assert path_findings[0].endswith(": 3")


# ---------------------------------------------------------------------------
# Cross-cutting behaviour
# ---------------------------------------------------------------------------


def test_scrubbed_content_no_residual_secrets_for_redactions() -> None:
    """Redactions must not leave behind the original substring."""
    text = "/home/operator/secret operator@example.com Maestro"
    result = scan_contents(text)
    assert "/home/operator/secret" not in result.scrubbed_content
    assert "operator@example.com" not in result.scrubbed_content
    assert "maestro" not in result.scrubbed_content.lower()


def test_hard_block_does_not_modify_scrubbed_content() -> None:
    """Hard-block matches are reported but not redacted."""
    text = "AKIAABCDEFGHIJKLMNOP"
    result = scan_contents(text)
    assert result.hard_block_findings
    # Scrubbed content is the input verbatim — hard blocks live in findings.
    assert result.scrubbed_content == text


def test_scan_dedupes_hard_block_findings_by_kind_and_preview() -> None:
    """Same kind + same preview repeated must collapse to one finding."""
    text = "AKIAABCDEFGHIJKLMNOP and again AKIAABCDEFGHIJKLMNOP"
    result = scan_contents(text)
    aws_findings = [
        f for f in result.hard_block_findings if f.startswith("aws_access_key:")
    ]
    assert len(aws_findings) == 1


def test_scan_distinct_previews_recorded_separately() -> None:
    """Same kind + different match values → two findings."""
    text = "AKIAABCDEFGHIJKLMNOP and AKIAQRSTUVWXYZABCDEF"
    result = scan_contents(text)
    aws_findings = [
        f for f in result.hard_block_findings if f.startswith("aws_access_key:")
    ]
    assert len(aws_findings) == 2


def test_scan_distinct_anthropic_keys_recorded_separately() -> None:
    # Two real-world-shaped Anthropic keys share the sk-ant-api03- prefix,
    # so the 12-char preview collapses to the same "sk-ant-api03" for both.
    # Dedupe must key on the full match, not the truncated preview, or the
    # second leak is silently dropped from hard_block_findings.
    key_a = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"
    key_b = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBB"
    result = scan_contents(f"{key_a} and {key_b}")
    anthropic_findings = [
        f for f in result.hard_block_findings if f.startswith("anthropic_key:")
    ]
    assert len(anthropic_findings) == 2


def test_has_hard_block_true() -> None:
    assert has_hard_block("AKIAABCDEFGHIJKLMNOP") is True


def test_has_hard_block_false() -> None:
    assert has_hard_block("nothing to see here") is False


def test_scan_empty_input() -> None:
    result = scan_contents("")
    assert result.hard_block_findings == []
    assert result.redact_findings == []
    assert result.scrubbed_content == ""


def test_truncate_preview_short_string_returned_as_is() -> None:
    """Match text at or below the preview budget is returned verbatim."""
    assert _truncate_preview("short") == "short"
    assert _truncate_preview("AAAAAAAAAAAA") == "AAAAAAAAAAAA"  # exactly 12


def test_truncate_preview_long_string_truncated_with_ellipsis() -> None:
    long_match = "A" * 50
    preview = _truncate_preview(long_match)
    assert preview == "A" * 12 + "..."
    assert len(preview) == 15


def test_scrub_readme_is_alias_for_scan_contents() -> None:
    text = "/tmp/foo Maestro"
    via_alias = scrub_readme(text)
    via_direct = scan_contents(text)
    assert via_alias == via_direct


def test_pattern_tuples_are_immutable() -> None:
    """Module-level patterns must be tuples so tests can rely on identity."""
    assert isinstance(HARD_BLOCK_PATTERNS, tuple)
    assert isinstance(REDACT_PATTERNS, tuple)


# ---------------------------------------------------------------------------
# scrub_file
# ---------------------------------------------------------------------------


def test_scrub_file_utf8(mem_fs: dict[str, str]) -> None:
    path = Path("/fake/doc.md")
    path.write_text("Hello /tmp/foo\n", encoding="utf-8")
    assert "/fake/doc.md" in mem_fs
    result = scrub_file(path)
    assert "<redacted-path>" in result.scrubbed_content
    assert "/tmp/foo" not in result.scrubbed_content


def test_scrub_file_binary_fallback_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-UTF-8 input must yield an empty result + warning, not raise."""
    path = Path("/fake/blob.bin")
    decode_err = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
    with patch.object(Path, "read_text", side_effect=decode_err), \
            caplog.at_level("WARNING", logger="maestro"):
        result = scrub_file(path)
    assert result == ScanResult()
    assert any("non-UTF-8" in rec.message for rec in caplog.records)


def test_scrub_file_missing_file_propagates() -> None:
    """A missing path must raise FileNotFoundError so callers can see real bugs."""
    path = Path("/fake/missing.md")
    with (
        patch.object(Path, "read_text", side_effect=FileNotFoundError(path)),
        pytest.raises(FileNotFoundError),
    ):
        scrub_file(path)


def test_scrub_file_permission_error_propagates() -> None:
    """A read-blocked path must raise PermissionError so callers see real bugs."""
    path = Path("/fake/forbidden.md")
    with (
        patch.object(Path, "read_text", side_effect=PermissionError(path)),
        pytest.raises(PermissionError),
    ):
        scrub_file(path)
