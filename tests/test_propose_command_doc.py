"""Regression: propose.md's redaction regex catches raw Telegram bot tokens.

The evidence-mining step in ``.claude/commands/propose.md`` tells an LLM to
redact Telegram bot tokens from any quoted store span before it lands in a
proposal doc — a doc the same command notes is "destined for a public PR". It
prescribes a concrete regex to do so.

A raw Telegram bot token is stored/validated in the form ``<digits>:<secret>``
(the validator in ``maestro/config.py`` is ``^\\d+:[A-Za-z0-9_-]+$``) — it has
no ``bot`` prefix. That prefix appears only when the token is embedded in an
API URL path (``.../bot<id>:<secret>/...``). A regex that *requires* the ``bot``
prefix therefore silently fails to redact a raw token quoted out of
``messages.jsonl``, ``usage.jsonl``, or an SDK/error log, leaking a live secret
into a public doc.

This test extracts the prescribed regex from the doc and proves it redacts the
raw stored form as well as the URL-path form, without over-redacting an
innocuous ``N:M`` ratio (the ``{30,}`` secret-length floor guards against that).
"""

from __future__ import annotations

import re
from pathlib import Path

_PROPOSE_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "propose.md"
)

# A raw Telegram bot token as stored in messages.jsonl / usage.jsonl and
# accepted by maestro/config.py's ^\d+:[A-Za-z0-9_-]+$ validator — no "bot"
# prefix; the secret part is well over the 30-char redaction floor.
_RAW_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawXYZ"
_URL_TOKEN = f"https://api.telegram.org/bot{_RAW_TOKEN}/sendMessage"


def _extract_redaction_regex(doc: str) -> str:
    # The doc mentions "Telegram bot tokens" twice — once with the concrete
    # backtick-quoted regex, once as generic prose. Pin the regex form.
    matches = re.findall(r"Telegram bot tokens \(`([^`]+)`\)", doc)
    assert len(matches) == 1, (
        f"Expected exactly one backtick-quoted Telegram bot-token redaction "
        f"regex in propose.md, found {len(matches)}."
    )
    return matches[0]


def test_redaction_regex_catches_raw_and_url_token_forms() -> None:
    pattern = _extract_redaction_regex(_PROPOSE_DOC.read_text(encoding="utf-8"))
    compiled = re.compile(pattern)

    # The raw stored form (no "bot" prefix) MUST be redacted — this is the form
    # actually quoted out of the JSONL stores and logs.
    assert compiled.search(_RAW_TOKEN) is not None, (
        f"Redaction regex {pattern!r} fails to match a raw Telegram bot token "
        f"{_RAW_TOKEN!r}; a live secret would leak into a public proposal."
    )
    # The URL-path form must still be caught.
    assert compiled.search(_URL_TOKEN) is not None, (
        f"Redaction regex {pattern!r} fails to match the URL-path token form."
    )


def test_redaction_regex_does_not_over_redact_short_ratios() -> None:
    pattern = _extract_redaction_regex(_PROPOSE_DOC.read_text(encoding="utf-8"))
    compiled = re.compile(pattern)

    # Innocuous "N:M" ratios / timestamps common in evidence spans must NOT be
    # redacted — otherwise the guidance over-redacts legitimate metrics.
    assert compiled.search("14 of 40 tasks; ratio 5:3 over 9 days") is None, (
        f"Redaction regex {pattern!r} over-matches an innocuous ratio."
    )
