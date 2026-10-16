"""Regression: `/review-changes` must bound its own untracked-file reads.

The `/review-changes` command enforces Maestro's context-bloat discipline on
every change it reviews — its step 5 "Context bloat prevention" bullet requires
that *any* data flowing into an LLM prompt be bounded. Yet the command's
diff-gathering step (step 3, uncommitted scope) once told the reviewer to
"Read each untracked file in full" with no size bound. A branch that
accidentally untracked a large generated artifact — a stray ``logs/`` file, a
``dist/`` or ``node_modules/`` tree that slipped ``.gitignore`` — would then
pull the entire file into the review context without limit, the command
breaking against itself the very rule it enforces on others (issue #1452).

This test pins the fix: the uncommitted-scope untracked-file read must now
carry an explicit size threshold plus skip/truncate language and a
large-artifact path-exclusion list, so a future edit cannot silently restore
the unbounded read.

Like the sibling ``*_command_doc.py`` tests, it reads a committed repo file
only — no ``mem_fs``, no subprocess, no network, no sleep.
"""

from __future__ import annotations

from pathlib import Path

_COMMANDS = Path(__file__).resolve().parent.parent / ".claude" / "commands"
_REVIEW_DOC = _COMMANDS / "review-changes.md"

# Large-artifact directories an accidentally-untracked generated tree lands
# under; the untracked read must exclude each so none balloons the review.
_LARGE_ARTIFACT_PATHS = (
    "logs/",
    "artifacts/",
    "node_modules/",
    "dist/",
    "__pycache__/",
    ".venv/",
)


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_untracked_read_is_size_bounded() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    # Isolate the diff-gathering step so the assertion targets the untracked
    # read, not one of the several unrelated "untracked" mentions elsewhere.
    section = _section_between(
        doc, "3. **Gather the diff.**", "4. **Understand the change.**"
    )

    # The unbounded instruction must be gone.
    assert "Read each untracked file in full." not in section, (
        "The unbounded 'Read each untracked file in full.' instruction must be "
        "replaced by a bounded read."
    )

    # An explicit size threshold with a KB token.
    assert "KB" in section, (
        "The untracked-file read must carry an explicit size threshold (KB)."
    )

    # Skip/truncate language so an oversized file is not read whole.
    lowered = section.lower()
    assert "skip" in lowered or "truncate" in lowered, (
        "The untracked-file read must skip or truncate an oversized file."
    )

    # A large-artifact path-exclusion list, so an accidentally-untracked
    # generated tree is not pulled into the review context.
    for artifact_path in _LARGE_ARTIFACT_PATHS:
        assert artifact_path in section, (
            "The untracked-file read must exclude the large-artifact path "
            f"{artifact_path!r}."
        )
