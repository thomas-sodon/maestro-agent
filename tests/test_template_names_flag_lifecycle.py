"""Regression: the proposal TEMPLATE names the four-stage flag lifecycle.

Proposal 117, step 5 — a new *flagged* feature must default to the disciplined
rollout the README authoring note already prescribes: dark-launch off
everywhere behind a default-off ``Config`` switch registered in
``EXPERIMENTAL_FLAGS`` with a dated ``review_by`` horizon, open a production
canary (a ``maestro.production.toml`` line plus a justified
``PROD_ONLY_CANARIES`` entry, with the base key tagged), observe for a bounded
window, then graduate and collapse via ``CUTOVER_FLAGS``. Left un-named in
``TEMPLATE.md`` — the surface an author or Maestro fills in — an ad-hoc
rollout stays equally reachable. This
test pins that the template's Implementation Plan guidance names the four
stages and the dark-launch-off-everywhere default, so a future edit that drops
the lifecycle vocabulary reds the build. It mirrors the single-file
``read_text(encoding="utf-8")`` doc-read idiom in
``tests/test_proposals_readme_order_numbering.py`` and the detector-fires /
detector-ignores negative-control house style in
``tests/test_no_line_citations_in_docs.py``.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "docs" / "proposals" / "TEMPLATE.md"
)

# The four stage phrases plus the dark-launch registration vocabulary the
# guidance must carry. Each is a stable phrase, not a line number, so the check
# survives unrelated edits to the template.
_REQUIRED_PHRASES = (
    "dark-launch off everywhere",                 # stage 1 — dark launch
    "open a production canary",                   # stage 2 — production canary
    "observe for a bounded window",               # stage 3 — temporal canary
    "CUTOVER_FLAGS",                              # stage 4 — collapse
    "EXPERIMENTAL_FLAGS",                          # dark-launch registration
    "review_by",                                   # dated horizon
)


def _missing_lifecycle_phrases(text: str) -> list[str]:
    """Return the required lifecycle phrases *absent* from ``text``.

    Whitespace is collapsed to single spaces before matching so a phrase that
    Markdown wraps across a line break (e.g. ``**open a production\\ncanary**``)
    still matches. An empty list means every phrase is present; a non-empty
    list names what the guidance is missing.
    """
    normalized = " ".join(text.split())
    return [phrase for phrase in _REQUIRED_PHRASES if phrase not in normalized]


def test_template_names_the_four_stage_flag_lifecycle() -> None:
    """``TEMPLATE.md`` must name all four lifecycle stages and the default."""
    assert _TEMPLATE.is_file(), (
        f"Expected {_TEMPLATE} to exist; the template was moved or renamed, "
        "so this guard would not validate anything."
    )
    text = _TEMPLATE.read_text(encoding="utf-8")
    # Non-empty guard: an empty read would make the assertion below pass
    # vacuously (nothing is missing from nothing).
    assert text.strip(), f"{_TEMPLATE} is empty; the scan would pass vacuously."
    missing = _missing_lifecycle_phrases(text)
    assert not missing, (
        "docs/proposals/TEMPLATE.md no longer names the four-stage flag "
        f"lifecycle; missing vocabulary: {missing}. Restore the dark-launch "
        "off-everywhere default (EXPERIMENTAL_FLAGS / review_by → production "
        "canary → bounded observation window → CUTOVER_FLAGS collapse) in the "
        "Implementation Plan guidance."
    )


def test_detector_fires_on_text_without_the_lifecycle() -> None:
    """Negative control: the detector must report every phrase missing from a
    string that names none of them.

    Without this, a detector regression (e.g. an emptied ``_REQUIRED_PHRASES``)
    would make the real-file assertion above pass vacuously.
    """
    missing = _missing_lifecycle_phrases("gate behaviour behind a config flag.")
    assert missing == list(_REQUIRED_PHRASES)
    # The vocabulary set itself must be non-empty, else every scan is vacuous.
    assert _REQUIRED_PHRASES


def test_detector_ignores_text_that_names_the_lifecycle() -> None:
    """A string carrying every stage phrase reports nothing missing."""
    seeded = (
        "dark-launch off everywhere behind a default-off Config switch "
        "registered in EXPERIMENTAL_FLAGS with a dated review_by horizon; "
        "open a production canary; observe for a bounded "
        "window against written tripwires; then collapse via CUTOVER_FLAGS."
    )
    assert _missing_lifecycle_phrases(seeded) == []
