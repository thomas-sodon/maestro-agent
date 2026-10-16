"""Secret scanning and content scrubbing for the publication pipeline.

The scanner has two distinct responsibilities applied in sequence:

* **Hard-block detection**: matches against a frozen tuple of regex patterns
  for credentials whose presence forbids publication entirely (API keys,
  PATs, AWS access keys, private keys, keyword-proximate base64 runs).
  Findings are reported but the scrubbed content is unchanged.
* **Inline redaction**: matches against a separate frozen tuple of regex
  patterns for content that is safe to publish in scrubbed form
  (developer-machine paths, owner email/host, internal product naming).
  Each match is replaced inline in the returned ``scrubbed_content``.

The narrative redactions (``maestro``, ``self-evolving``,
``autonomous AI agent``) are **publication-specific** — they exist to mask
the project's internal naming when artifacts are surfaced as public
GitHub repos. Do not reuse this module as a general-purpose log scrubber.

That prohibition covers the *narrative* redactions only. The
credential-material redactor :func:`redact_credentials` is deliberately
general — it applies the hard-block credential regexes plus
:data:`CREDENTIAL_LOG_PATTERNS` and nothing else, so log-shaped text
(CI run logs, subprocess stderr) can be scrubbed of tokens before it
reaches an artifact, a prompt, or the journal.

This module ships in proposal 030 as infrastructure only. Phase 030 has no
call sites; proposals 031 and 032 will consume it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("maestro")


# ---------------------------------------------------------------------------
# Hard-block patterns
# ---------------------------------------------------------------------------
#
# Tuple order matters: anthropic precedes openai because the negative
# lookahead in ``openai_key`` (``(?!ant-)``) deliberately prevents the
# OpenAI pattern from claiming the prefix of an Anthropic key, but the
# anthropic pattern must be the one that records the finding.
#
# The ``github_pat_fine_grained`` entry extends the proposal's spec to
# cover GitHub's modern fine-grained personal access tokens
# (``github_pat_<82..255 chars>``). A leak of one of these is no less
# severe than the classic ``ghp_`` form.

HARD_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{32,}\b")),
    ("github_token_classic", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github_pat_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,255}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # PGP armor uses the ``PRIVATE KEY BLOCK`` terminus per RFC 4880, while
    # the OpenSSH/RSA/EC/DSA formats end at ``PRIVATE KEY``. Both shapes
    # must match — without the ``BLOCK`` alternative real PGP exports
    # would publish without triggering this hard block. The third shape is
    # the generic PKCS#8 container (``PRIVATE KEY`` / ``ENCRYPTED PRIVATE
    # KEY``) with no algorithm prefix — what most modern tooling writes.
    (
        "private_key",
        re.compile(
            r"-----BEGIN "
            r"(?:(?:OPENSSH|RSA|EC|DSA) PRIVATE KEY|PGP PRIVATE KEY BLOCK"
            r"|(?:ENCRYPTED )?PRIVATE KEY)"
            r"-----",
        ),
    ),
    # Keyword-proximate base64 — at least 80 chars of base64 within 32
    # characters after a credential keyword. The keyword must come first
    # so a benign 200-char base64 with no nearby keyword does not match.
    (
        "keyword_b64",
        re.compile(
            r"(?i)(?:password|token|secret|api[_-]?key)"
            r"[\s\S]{0,32}[A-Za-z0-9+/=]{80,}",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Inline redaction patterns
# ---------------------------------------------------------------------------
#
# Each entry is ``(finding_kind, pattern, replacement)``. Findings are
# aggregated by ``finding_kind`` with a count, so redact_findings looks
# like ``["redacted-path: 3", "narrative_redactions:name_maestro: 1"]``.

REDACT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("redacted-path", re.compile(r"/home/[A-Za-z0-9._-]+/[^\s,;:)\]}'\"!?>`]+"), "<redacted-path>"),
    ("redacted-path", re.compile(r"/workspace/[^\s,;:)\]}'\"!?>`]+"), "<redacted-path>"),
    ("redacted-path", re.compile(r"/tmp/[^\s,;:)\]}'\"!?>`]+"), "<redacted-path>"),
    (
        "redacted-email",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
        "<redacted-email>",
    ),
    (
        "narrative_redactions:name_maestro",
        re.compile(r"(?i)\bmaestro\b"),
        "<redacted>",
    ),
    (
        "narrative_redactions:self_phrase",
        re.compile(
            r"(?i)self[-\s]?(?:evolving|reflecting|modifying|awareness|aware)",
        ),
        "<redacted>",
    ),
    (
        "narrative_redactions:autonomous_agent",
        re.compile(r"(?i)autonomous\s+(?:ai\s+)?agent"),
        "<redacted>",
    ),
)


# ---------------------------------------------------------------------------
# Credential-material log patterns
# ---------------------------------------------------------------------------
#
# Credential shapes that ``HARD_BLOCK_PATTERNS`` does not cover but that do
# appear in log-shaped text: an HTTP ``Bearer`` token and a Telegram bot
# token (``<id>:<secret>``). Used only by :func:`redact_credentials`.

CREDENTIAL_LOG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"(?:bot)?\d+:[A-Za-z0-9_-]{30,}"),
)


def redact_credentials(text: str) -> str:
    """Substitute credential material in *text* with ``<redacted>``.

    Applies the :data:`HARD_BLOCK_PATTERNS` credential regexes plus
    :data:`CREDENTIAL_LOG_PATTERNS`, so a leaked token in log-shaped text
    (a CI run log, subprocess stderr) never reaches an artifact, a prompt,
    or the journal. Pure, and deliberately *not* a narrative redactor —
    :data:`REDACT_PATTERNS` stays publication-only.
    """
    for _kind, pattern in HARD_BLOCK_PATTERNS:
        text = pattern.sub("<redacted>", text)
    for pattern in CREDENTIAL_LOG_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


# Maximum characters of a hard-block match included in the finding preview.
# We want enough context to identify the finding kind in logs without
# leaking a usable fragment of the credential.
_PREVIEW_MAX_CHARS = 12


@dataclass
class ScanResult:
    """Outcome of a single scan invocation.

    ``hard_block_findings`` lists each hard-block match as
    ``"{kind}: {truncated_preview}..."``. ``redact_findings`` aggregates
    the inline redactions as ``"{kind}: {count}"`` (count > 0).
    ``scrubbed_content`` is the input with every redaction applied; it is
    unchanged by hard-block matches (those only contribute to findings).
    """

    hard_block_findings: list[str] = field(default_factory=list)
    redact_findings: list[str] = field(default_factory=list)
    scrubbed_content: str = ""


def has_hard_block(text: str) -> bool:
    """Return ``True`` iff *text* contains any hard-block pattern.

    Fast pre-check for callers that want to short-circuit before doing
    the full scan + redact work. Equivalent to
    ``bool(scan_contents(text).hard_block_findings)`` but skips the
    redaction pass.
    """
    return any(pattern.search(text) for _kind, pattern in HARD_BLOCK_PATTERNS)


def _truncate_preview(match_text: str) -> str:
    """Truncate a hard-block match for inclusion in a finding string.

    Hard-block findings flow into log messages and (eventually) Telegram
    notifications. We keep just enough of the matched text to identify
    the credential kind without leaking a usable substring.
    """
    if len(match_text) <= _PREVIEW_MAX_CHARS:
        return match_text
    return match_text[:_PREVIEW_MAX_CHARS] + "..."


def scan_contents(text: str) -> ScanResult:
    """Scan *text* for hard-block credentials and apply inline redactions.

    Hard-block matches are deduplicated per kind by recording each
    distinct match preview only once. Redact findings are aggregated
    by kind with a total count.
    """
    hard_blocks: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in HARD_BLOCK_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0)
            key = (kind, raw)
            if key in seen:
                continue
            seen.add(key)
            hard_blocks.append(f"{kind}: {_truncate_preview(raw)}")

    redact_counts: dict[str, int] = {}
    scrubbed = text
    for kind, pattern, replacement in REDACT_PATTERNS:
        scrubbed, count = pattern.subn(replacement, scrubbed)
        if count:
            redact_counts[kind] = redact_counts.get(kind, 0) + count

    redact_findings = [f"{kind}: {count}" for kind, count in redact_counts.items()]

    return ScanResult(
        hard_block_findings=hard_blocks,
        redact_findings=redact_findings,
        scrubbed_content=scrubbed,
    )


def scrub_readme(readme: str) -> ScanResult:
    """Alias for :func:`scan_contents` used by README-publication call sites.

    Distinct symbol so future README-specific behaviour (e.g. extra
    front-matter scrubbing) can land here without touching the generic
    :func:`scan_contents` consumers.
    """
    return scan_contents(readme)


def scrub_file(path: Path) -> ScanResult:
    """Read *path* as UTF-8 and scan it.

    Binary files (those that cannot be decoded as UTF-8) yield an empty
    :class:`ScanResult` and a logged warning rather than raising — the
    publication pipeline treats unscanable binaries as non-publishable
    elsewhere. All other I/O errors — notably :class:`FileNotFoundError`
    and :class:`PermissionError` — propagate so real bugs surface;
    callers must guard the missing-path case themselves.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.warning("Skipping non-UTF-8 file in scrub_file: %s", path)
        return ScanResult()
    return scan_contents(text)
