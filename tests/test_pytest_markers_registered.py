"""Guard the ``[tool.pytest.ini_options].markers`` registry in ``pyproject.toml``.

``tests/browser/test_public_page.py`` decorates cases with
``@pytest.mark.browser_context_args(...)`` — a custom mark that
``pytest-playwright`` supplies only at browser-run time. In the unit
environment that plugin is absent, so an unregistered mark emits a
``PytestUnknownMarkWarning`` at collection. Registering the marker in
``pyproject.toml`` silences that warning deterministically.

This deterministic, zero-LLM-cost config scan parses the real
``pyproject.toml`` with ``tomllib`` (no ``mem_fs``, network, or subprocess)
and pins that ``browser_context_args`` stays registered alongside the
pre-existing ``component`` and ``browser`` marks, and that every custom
``pytest.mark.<name>`` used by the browser page test is registered — so a
future edit cannot silently reintroduce the unknown-mark warning.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_BROWSER_TEST = _REPO_ROOT / "tests" / "browser" / "test_public_page.py"


def _registered_marker_names() -> set[str]:
    """Return the bare marker names declared in ``[tool.pytest.ini_options]``."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    # Each entry is ``"<name>: <description>"`` — keep the name before the colon.
    return {entry.split(":", 1)[0].strip() for entry in markers}


def test_browser_context_args_marker_registered() -> None:
    """The pytest-playwright ``browser_context_args`` mark is registered."""
    assert "browser_context_args" in _registered_marker_names(), (
        "browser_context_args must be registered in "
        "[tool.pytest.ini_options].markers to silence PytestUnknownMarkWarning "
        "when tests/browser/test_public_page.py is collected in the unit env."
    )


def test_preexisting_markers_remain_registered() -> None:
    """Adding ``browser_context_args`` did not disturb ``component``/``browser``."""
    registered = _registered_marker_names()
    assert "component" in registered
    assert "browser" in registered


def test_all_custom_marks_in_browser_test_are_registered() -> None:
    """No custom ``pytest.mark.<name>`` in the browser test is unregistered.

    An unregistered custom mark is exactly what raises
    ``PytestUnknownMarkWarning`` at collection, so pinning that every mark
    used by the collected file is registered is a direct proxy for
    "collection emits no unknown-mark warning".
    """
    source = _BROWSER_TEST.read_text(encoding="utf-8")
    assert source, f"Expected a non-empty browser test at {_BROWSER_TEST}"
    used_marks = set(re.findall(r"@pytest\.mark\.([A-Za-z_][A-Za-z0-9_]*)", source))
    # Vacuous-pass guard: the file is known to use browser_context_args.
    assert "browser_context_args" in used_marks, (
        "Expected browser_context_args usage in the browser test; the guard "
        "would pass hollowly if the mark were removed from the test file."
    )
    # ``parametrize``/``skip``/``skipif``/``usefixtures`` etc. are built-in
    # marks pytest registers itself; only project-custom marks need declaring.
    builtin_marks = {
        "parametrize",
        "skip",
        "skipif",
        "xfail",
        "usefixtures",
        "filterwarnings",
        "asyncio",
    }
    registered = _registered_marker_names()
    unregistered = used_marks - builtin_marks - registered
    assert not unregistered, (
        "These custom marks used in the browser test are not registered in "
        f"pyproject.toml and would emit PytestUnknownMarkWarning: {unregistered}"
    )
