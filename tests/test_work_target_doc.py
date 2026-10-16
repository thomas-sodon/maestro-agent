"""Guard: the ``[work]`` docs track the shipped work-target readers (issue #4223).

Proposal 107 shipped a live reader for every ``WorkTarget`` facet, yet the
``maestro.toml`` header, the ``docs/modules.md`` row, and several docstrings kept
describing an earlier increment in which the facets were "inert" and "nothing
reads" them — misleading an operator into treating ``focus_profile`` as a
placeholder when ``external_delivery`` in fact suppresses self-evolution. These
tests pin that drift class: no such site may call a facet inert, and the row must
name each facet reader.
"""

from __future__ import annotations

import ast
import re

from maestro.loop.lifecycle import _FOCUS_SUPPRESSED_PHASES
from tests.test_module_doc_coverage import _MAESTRO_ROOT, _MODULES_DOC, _REPO_ROOT
from tests.test_reflection_retrieval_doc import _method_docstring

_ROW = re.compile(r"^\|\s*`maestro/work_target\.py`.*$", re.MULTILINE)
_INERT = re.compile(r"inert|nothing reads|land later|later increment", re.IGNORECASE)
_WORK_HEADER = "[work]"


def _header_comment_block(toml_text: str) -> str:
    """Return the contiguous ``#`` comment lines directly above ``[work]``, or ``""``."""
    lines = toml_text.splitlines()
    if _WORK_HEADER not in lines:
        return ""
    block: list[str] = []
    for line in reversed(lines[: lines.index(_WORK_HEADER)]):
        if not line.startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def test_header_comment_block_extracts_and_falls_back() -> None:
    """The extractor stops at the first non-comment line and tolerates a missing header."""
    text = "x = 1\n# one\n# two\n[work]\na = 2\n"
    assert _header_comment_block(text) == "# one\n# two"
    assert _header_comment_block("# lone\n[github]\n") == ""


def test_inert_regex_flags_each_stale_phrase() -> None:
    """The drift regex catches every stale phrasing and passes live prose."""
    for phrase in ("stay Inert", "nothing reads it", "land later", "later increments"):
        assert _INERT.search(phrase) is not None
    assert _INERT.search("every facet is live") is None


def test_work_toml_header_claims_no_inert_facet() -> None:
    """The operator-facing ``[work]`` header must not call any facet inert."""
    block = _header_comment_block((_REPO_ROOT / "maestro.toml").read_text(encoding="utf-8"))
    assert block != ""
    assert _INERT.search(block) is None


def test_work_toml_header_names_every_suppressed_phase() -> None:
    """The header's ``external_delivery`` phase list matches ``_FOCUS_SUPPRESSED_PHASES``."""
    block = _header_comment_block((_REPO_ROOT / "maestro.toml").read_text(encoding="utf-8"))
    assert len(_FOCUS_SUPPRESSED_PHASES) > 0
    for phase in sorted(_FOCUS_SUPPRESSED_PHASES):
        assert phase in block


def test_config_work_field_comment_claims_no_inert_facet() -> None:
    """The ``work_*`` field comment in ``config.py`` calls no facet inert."""
    lines = (_MAESTRO_ROOT / "config.py").read_text(encoding="utf-8").splitlines()
    anchor = next(i for i, line in enumerate(lines) if line.strip().startswith("work_focus_profile:"))
    comment: list[str] = []
    for line in reversed(lines[:anchor]):
        if not line.strip().startswith("#"):
            break
        comment.append(line)
    assert comment != []
    assert _INERT.search("\n".join(comment)) is None


def test_work_target_modules_row_claims_no_inert_facet() -> None:
    """The modules.md row names every facet reader and calls none inert."""
    match = _ROW.search(_MODULES_DOC.read_text(encoding="utf-8"))
    assert match is not None
    row = match.group(0)
    assert _INERT.search(row) is None
    for reader in ("_FOCUS_SUPPRESSED_PHASES", "select_task_generation_prompt"):
        assert reader in row


def test_work_target_docstrings_claim_no_inert_facet() -> None:
    """The owner, its test module, and ``Config.work_target`` docstrings call no facet inert."""
    for path in (_MAESTRO_ROOT / "work_target.py", _REPO_ROOT / "tests" / "test_work_target.py"):
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        assert docstring != ""
        assert _INERT.search(docstring) is None
    config_source = (_MAESTRO_ROOT / "config.py").read_text(encoding="utf-8")
    method_doc = _method_docstring(config_source, "Config", "work_target")
    assert method_doc != ""
    assert _INERT.search(method_doc) is None
