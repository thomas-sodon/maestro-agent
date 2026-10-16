"""Regression: proposal docs must not label an existing file as ``new``.

Proposal 062 (``docs/proposals/core/062-cli-paced-slash-command-metrics.md``)
Step 2 described the work as creating **new**
``maestro_sdk/src/maestro_sdk/slash_probe.py`` ``+``
``maestro_sdk/src/maestro_sdk/types.py``. The leading word ``new`` governed
*both* file names through the ``+`` list, but only ``slash_probe.py`` was
genuinely new: ``maestro_sdk/src/maestro_sdk/types.py`` already existed and
defines the SDK's entire public data contract (``QueryOptions``, ``QueryUsage``,
``QueryTiming``, ``ToolCall``, ``QueryResult``, ``StreamDelta``), which Step 2
merely *extended* with the three report dataclasses. Read literally as a
create-both-files instruction, the step tells an implementer to create
``types.py`` from scratch, overwriting the file and silently deleting the public
contract every SDK caller depends on.

Banning the ``new ... `slash_probe.py` + `types.py``` create-list construct
deterministically kills the recurrence at zero LLM cost — the same guard family
as ``tests/test_no_line_citations_in_docs.py`` and
``tests/test_proposal_sdk_surface_names.py``. The corrected phrasing
(``new `slash_probe.py`, extending the existing `types.py```) drops the ``+``
coupling, so it does not match. The ``Affects`` summary row's comma-separated
``{session,types}.py`` brace list and the README's ``slash_probe.py (parses ...
into ... types.py) + Session.run_command`` construct also do not match: the ban
keys strictly on ``slash_probe.py`` being coupled to ``types.py`` by ``+`` under
a leading ``new``.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROPOSALS_DIR = Path(__file__).resolve().parent.parent / "docs" / "proposals"

# Flags an existing file being labeled ``new`` via a ``+`` create-list: the
# leading word ``new``, then the backtick-quoted ``slash_probe.py`` path, then a
# ``+`` list separator, then the backtick-quoted ``types.py`` path. ``[^`]*``
# spans only the whitespace between ``new`` and the first backtick (it cannot
# cross a backtick), so a distant ``new`` elsewhere in the doc cannot bridge to
# this path. The corrected ``..., extending the existing ...`` phrasing replaces
# the ``+`` with ``,`` and so does not match.
_NEW_EXISTING_FILE_RE = re.compile(
    r"\bnew\b[^`]*`maestro_sdk/src/maestro_sdk/slash_probe\.py`"
    r"\s*\+\s*`maestro_sdk/src/maestro_sdk/types\.py`"
)


def _find_new_existing_file_couplings(text: str) -> list[str]:
    """Return every ``new ... `slash_probe.py` + `types.py``` construct.

    Whitespace (including newlines, on which the construct is split in the
    source doc) is collapsed to single spaces first so the regex matches
    regardless of how the list wraps across lines.
    """
    normalized = re.sub(r"\s+", " ", text)
    return _NEW_EXISTING_FILE_RE.findall(normalized)


def test_proposals_do_not_label_existing_types_py_as_new() -> None:
    """No ``docs/proposals/**/*.md`` may couple the existing ``types.py`` to ``new``."""
    md_files = sorted(_PROPOSALS_DIR.rglob("*.md"))
    # Python 3.12 rglob() silently yields nothing for a missing directory
    # instead of raising, so without this guard the scan below would pass
    # vacuously (zero files checked) if docs/proposals/ ever went missing.
    assert md_files, (
        f"Expected at least one .md file under {_PROPOSALS_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[tuple[str, list[str]]] = []
    for path in md_files:
        hits = _find_new_existing_file_couplings(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(_PROPOSALS_DIR.parent.parent)
            offenders.append((str(rel), hits))
    assert not offenders, (
        "Proposal docs label the existing maestro_sdk/src/maestro_sdk/types.py "
        "as a 'new' file via a '+' create-list: "
        + "; ".join(f"{rel}: {hits}" for rel, hits in offenders)
        + ". types.py already defines the SDK's public data contract "
        "(QueryOptions, QueryUsage, QueryTiming, ToolCall, QueryResult, "
        "StreamDelta); it is extended, not created. Reword to 'new "
        "`slash_probe.py`, extending the existing `types.py`'."
    )


def test_detector_fires_on_seeded_bad_construct() -> None:
    """Negative control: the detector must catch the seeded create-both-files form.

    The seed splits the list across a newline (as the source doc does) to prove
    the whitespace normalization bridges the line break; without this control a
    detector regression (e.g. a broken regex) would make the clean-tree
    assertion above pass vacuously.
    """
    text = (
        "*(Landed — PR #2512.)* new\n"
        "   `maestro_sdk/src/maestro_sdk/slash_probe.py` +\n"
        "   `maestro_sdk/src/maestro_sdk/types.py` — a marker/regex pack"
    )
    hits = _find_new_existing_file_couplings(text)
    assert hits == [
        "new `maestro_sdk/src/maestro_sdk/slash_probe.py` + "
        "`maestro_sdk/src/maestro_sdk/types.py`"
    ]


def test_detector_passes_corrected_extending_phrasing() -> None:
    """Positive control: the corrected 'extending the existing' phrasing is allowed.

    Dropping the ``+`` coupling for ``, extending the existing`` names
    ``types.py`` as the file being extended, not created, so the detector must
    not fire.
    """
    text = (
        "*(Landed — PR #2512.)* new\n"
        "   `maestro_sdk/src/maestro_sdk/slash_probe.py`, extending the existing\n"
        "   `maestro_sdk/src/maestro_sdk/types.py` — a marker/regex pack"
    )
    assert _find_new_existing_file_couplings(text) == []
