"""Structural guard for the root Dockerfile.

There is no Dockerfile linter in the quality gates (ruff/mypy/pytest are
Python-only), so cosmetic drift in the Dockerfile — like a doubled blank line
left behind when an intermediate build stage is removed — can otherwise slip in
silently. This meta-test reads the committed ``Dockerfile`` read-only and asserts
its stage boundaries use a single blank-line separator throughout, matching the
file's own convention.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from maestro.runtime_contract import load_contract

_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)

# Launcher binary → the Debian package token(s) the Dockerfile must install to
# provision it — the concrete-package expression of ``MCP_LAUNCHER_INSTALL_HINTS``'
# launcher→toolchain intent, for the launchers whose toolchain is apt-provisioned.
# ``npx`` ships with ``npm`` (which pulls ``nodejs``); under
# ``--no-install-recommends`` ``nodejs`` alone won't pull ``npm``, so both are
# required.
#
# ``uvx`` is intentionally *not* mapped: it ships with ``uv``, which is provisioned
# via pip / the astral installer (see ``MCP_LAUNCHER_INSTALL_HINTS``,
# https://docs.astral.sh/uv/), NOT apt — Debian (python:3.14-slim is trixie) has
# no ``uv`` package, so an ``apt-get install uv`` remediation would be
# unsatisfiable. Add ``uvx`` handling only alongside a matching (non-apt)
# Dockerfile check.
_LAUNCHER_APT_PACKAGES: dict[str, tuple[str, ...]] = {
    "npx": ("nodejs", "npm"),
}


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _apt_installed_packages(text: str | None = None) -> set[str]:
    """Package tokens installed via ``apt-get install`` in the Dockerfile.

    Joins backslash line-continuations into single logical commands, splits on
    the shell ``&&``/``;`` separators, then collects the non-flag tokens of every
    ``apt-get install`` invocation. Scoping the parity guard to this set — rather
    than to ``re.search`` over the whole file text — means a package named only
    in a comment (or any other non-install line) can never satisfy the guard.
    """
    source = _dockerfile_text() if text is None else text
    joined = re.sub(r"\\\n\s*", " ", source)
    packages: set[str] = set()
    for segment in re.split(r"&&|;", joined):
        if "apt-get install" not in segment:
            continue
        tokens = segment.split()
        # Everything after the ``install`` verb is a package name or a flag;
        # keep the names (flags start with ``-``).
        start = tokens.index("install")
        packages.update(t for t in tokens[start + 1 :] if not t.startswith("-"))
    return packages


def _pip_editable_sibling_dirs(text: str) -> set[str]:
    """Sibling package directories installed editable via ``pip install -e`` in *text*.

    Mirrors ``_apt_installed_packages``' parsing shape: joins backslash
    line-continuations into single logical commands, splits on the shell
    ``&&``/``;`` separators, then — only within segments that actually run
    ``pip install`` — walks the tokens collecting the argument after each ``-e``
    flag. A target's leading ``maestro_<name>`` component (e.g. ``maestro_sdk/`` or
    ``maestro_review/[dev]``) is the sibling directory; the main package target
    (``.``/``.[dev,email]``) has no such component and is ignored. Scoping to real
    ``pip install`` segments — never a bare ``re.search`` over the file text —
    means a sibling named only in a comment can never satisfy the guard.
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    siblings: set[str] = set()
    for segment in re.split(r"&&|;", joined):
        if "pip install" not in segment:
            continue
        tokens = segment.split()
        for i, tok in enumerate(tokens):
            if tok != "-e" or i + 1 >= len(tokens):
                continue
            target = tokens[i + 1].strip("\"'")
            match = re.match(r"(maestro_[A-Za-z0-9_]+)(?:/|\[|$)", target)
            if match:
                siblings.add(match.group(1))
    return siblings


def _required_apt_packages(flat: dict[str, object]) -> set[str]:
    """Apt packages the Dockerfile must provide for the enabled MCP launchers.

    Reads the production-merged ``[mcp]`` config and, for each *enabled* external
    MCP server, resolves its launcher to the Debian packages that ship it via
    ``_LAUNCHER_APT_PACKAGES``. A launcher enabled in production that is not mapped
    there fails loud — but the guidance points at the right remediation: an
    apt-provisioned launcher belongs in the map, whereas a non-apt one (like
    ``uvx``, whose ``uv`` ships via pip / the astral installer) needs its own
    Dockerfile check, never an unsatisfiable ``apt-get install``.
    """
    from maestro.config import EXTERNAL_MCP_PREFIXES

    required: set[str] = set()
    for prefix in EXTERNAL_MCP_PREFIXES:
        if not flat[f"{prefix}_mcp_enabled"]:
            continue
        launcher = flat[f"{prefix}_mcp_command"]
        assert launcher in _LAUNCHER_APT_PACKAGES, (
            f"external MCP server {prefix!r} is enabled in production with "
            f"launcher {launcher!r}, which this apt-parity guard does not map. If "
            f"the launcher is apt-provisioned, add it to _LAUNCHER_APT_PACKAGES; if "
            f"its toolchain ships via pip / the astral installer (e.g. uvx→uv, see "
            f"MCP_LAUNCHER_INSTALL_HINTS), add a matching Dockerfile check instead "
            f"of an apt package"
        )
        required.update(_LAUNCHER_APT_PACKAGES[launcher])
    return required


def test_dockerfile_exists() -> None:
    assert _DOCKERFILE.is_file(), f"expected {_DOCKERFILE} to exist"


def test_dockerfile_read_uses_explicit_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Dockerfile is read as UTF-8 regardless of the platform default.

    The committed Dockerfile contains U+2014 em-dashes in its stage comments, so
    reading it under a non-UTF-8 default locale (a C/POSIX runner, Windows
    cp1252) raises ``UnicodeDecodeError`` unless ``encoding`` is passed
    explicitly — the project's "all text I/O is UTF-8" invariant. This simulates
    an ASCII default by forcing ``io.text_encoding`` to resolve a missing
    encoding to ``ascii``; ``_dockerfile_text()`` must still decode the file
    because it passes ``encoding="utf-8"`` (it crashed here before the fix).
    """
    real_text_encoding = io.text_encoding

    def _ascii_default(encoding: str | None, stacklevel: int = 2) -> str:
        if encoding is None:
            return "ascii"
        return real_text_encoding(encoding, stacklevel)

    monkeypatch.setattr(io, "text_encoding", _ascii_default)

    text = _dockerfile_text()
    assert "—" in text  # em-dash survives the round-trip under an ASCII default


def test_no_consecutive_blank_lines() -> None:
    """No run of two-or-more consecutive blank lines.

    Every stage boundary in the Dockerfile uses exactly one blank line, so a pair
    of adjacent empty (after-strip) lines is the cosmetic drift this guard catches.
    """
    lines = _dockerfile_text().splitlines()
    doubled = [
        i
        for i in range(1, len(lines))
        if not lines[i].strip() and not lines[i - 1].strip()
    ]
    assert not doubled, (
        "Dockerfile has consecutive blank lines at 1-based line(s) "
        f"{[i + 1 for i in doubled]}; collapse each run to a single blank line."
    )


def test_production_mcp_launchers_provisioned() -> None:
    """The production image provisions every enabled external MCP launcher.

    ``maestro.production.toml`` turns the Context7 canary on
    (``[mcp] context7_enabled = true``), whose launcher is ``npx``. If the
    committed ``Dockerfile`` does not install the toolchain that ships that
    launcher, ``Config.check_mcp_launchers`` fails preflight on every deploy and
    triggers an automatic rollback, freezing production (issue #3054). This
    config↔image parity guard reads the production-merged ``[mcp]`` section and
    the committed Dockerfile read-only and asserts that each enabled external
    launcher's Debian package is provisioned — closing the gap between what the
    config declares and what the image ships.
    """
    import dataclasses

    from maestro.config import Config
    from maestro.config_settings import build_config_via_pydantic

    repo_root = _DOCKERFILE.parent
    config = build_config_via_pydantic(repo_root, env_name="production")
    flat = {f.name: getattr(config, f.name) for f in dataclasses.fields(Config)}

    required_packages = _required_apt_packages(flat)

    installed = _apt_installed_packages()
    for package in sorted(required_packages):
        assert package in installed, (
            f"an enabled production MCP launcher requires the {package!r} apt "
            f"package, but the committed Dockerfile does not apt-get install it "
            f"(installed: {sorted(installed)}); add it to the base-stage "
            f"apt-get install list so preflight's check_mcp_launchers passes "
            f"(issue #3054)"
        )


def test_non_apt_launcher_requires_dockerfile_check_not_apt() -> None:
    """An enabled launcher with no apt mapping earns non-apt remediation.

    serena's ``uvx`` launcher provisions ``uv`` via pip / the astral installer,
    not apt, so ``uvx`` is intentionally absent from ``_LAUNCHER_APT_PACKAGES``
    (Debian has no ``uv`` package). serena is disabled in production today, so this
    path is dormant — but if it were ever enabled, the parity guard must still fail
    loud while pointing at a Dockerfile check, never the unsatisfiable
    ``apt-get install uv`` the old ``uvx→uv`` mapping would have demanded.
    """
    from maestro.config import EXTERNAL_MCP_PREFIXES

    flat: dict[str, object] = {
        f"{prefix}_mcp_enabled": False for prefix in EXTERNAL_MCP_PREFIXES
    }
    flat.update({f"{prefix}_mcp_command": "npx" for prefix in EXTERNAL_MCP_PREFIXES})
    flat["serena_mcp_enabled"] = True
    flat["serena_mcp_command"] = "uvx"

    with pytest.raises(AssertionError) as excinfo:
        _required_apt_packages(flat)

    message = str(excinfo.value)
    assert "uvx" in message
    assert "Dockerfile check" in message
    assert "apt-get install uv" not in message


def test_sibling_editable_installs_match_ci() -> None:
    """The image installs every sibling package the CI workflow installs editable.

    ``maestro_sdk/`` and ``maestro_review/`` are excluded from maestro's own
    distribution (``pyproject.toml`` ``packages.find`` exclude) and must be
    installed separately. CI does so explicitly
    (``.github/workflows/quality-gates.yml``), and ``docs/deployment.md`` documents
    the same step — but the loop container builds its own image, so if the
    Dockerfile omits the sibling install, in-container quality-gate runs abort at
    collection (``import maestro_sdk.mcp_config`` resolves to a half-empty PEP 420
    namespace under ``PYTHONPATH=/workspace``) while CI stays green, demoting every
    plan on a red the code did not cause (issue #3872). This parity guard reads the
    committed workflow and Dockerfile read-only and asserts each sibling CI installs
    editable has a counterpart ``pip install`` target in the image.
    """
    ci_text = _WORKFLOW.read_text(encoding="utf-8")
    ci_siblings = _pip_editable_sibling_dirs(ci_text)
    # Pin the CI parse against the known sibling set so the parity assertion below
    # cannot pass vacuously if the parser stops matching the workflow.
    assert {"maestro_sdk", "maestro_review"} <= ci_siblings, (
        f"expected the CI workflow to install maestro_sdk and maestro_review "
        f"editable, but the parser found {sorted(ci_siblings)}; the sibling-install "
        f"parser has drifted from .github/workflows/quality-gates.yml"
    )

    dockerfile_siblings = _pip_editable_sibling_dirs(_dockerfile_text())
    missing = ci_siblings - dockerfile_siblings
    assert not missing, (
        f"the CI workflow installs the sibling package(s) {sorted(missing)} editable "
        f"but the committed Dockerfile does not; add a matching "
        f'`pip install -e "<sibling>/"` to the base stage so in-container quality '
        f"gates can collect (issue #3872). Dockerfile installs: "
        f"{sorted(dockerfile_siblings)}"
    )


def test_sibling_install_parity_guard_catches_missing_install() -> None:
    """Seeded-offender control: the parser detects a stripped sibling install.

    Feeds ``_pip_editable_sibling_dirs`` a Dockerfile text with the sibling
    editable install removed (only the main-package install remains) and asserts
    the parser reports it absent — proving ``test_sibling_editable_installs_match_ci``
    genuinely fails when the image drops the sibling install, rather than passing
    vacuously because the parser silently stopped matching ``pip install -e``.
    """
    stripped_dockerfile = (
        "COPY maestro/ maestro/\n"
        # maestro_sdk is named only in a comment here — it must NOT satisfy the
        # guard, since the guard scopes token collection to real pip install
        # segments rather than a bare text search.
        "# maestro_sdk/ is a sibling package installed elsewhere\n"
        'RUN pip install --no-cache-dir -c constraints.txt ".[dev,email]"\n'
    )
    dockerfile_siblings = _pip_editable_sibling_dirs(stripped_dockerfile)
    assert "maestro_sdk" not in dockerfile_siblings

    ci_text = _WORKFLOW.read_text(encoding="utf-8")
    ci_siblings = _pip_editable_sibling_dirs(ci_text)
    missing = ci_siblings - dockerfile_siblings
    assert "maestro_sdk" in missing


def _contract_toolbelt_apt_packages() -> set[str]:
    """Packages the shipped contract's ``toolbelt`` entries provision via apt."""
    packages: set[str] = set()
    for entry in load_contract().entries:
        if entry.kind != "toolbelt":
            continue
        tokens = entry.provision.split()
        if tokens[:2] != ["apt-get", "install"]:
            continue
        names = [token for token in tokens[2:] if not token.startswith("-")]
        if not names:
            raise AssertionError(f"unparseable apt provision: {entry.provision!r}")
        packages.update(names)
    return packages


def test_contract_toolbelt_apt_packages_installed() -> None:
    """Every apt-provisioned toolbelt entry of the runtime contract is installed.

    ``deploy/runtime_contract.toml`` requires the shell toolbelt in production;
    an image that drops one of its packages makes ``maestro runtime-contract``
    read ``violated`` and the agent's tool calls fail ``command not found``.
    """
    expected = _contract_toolbelt_apt_packages()
    assert {"procps", "jq"} <= expected
    missing = expected - _apt_installed_packages()
    assert missing == set()


def test_contract_toolbelt_guard_catches_missing_install() -> None:
    """Seeded-offender control: a Dockerfile without ``jq`` is reported missing."""
    stripped_dockerfile = (
        "# jq is named only in a comment and must not satisfy the guard\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "        git \\\n"
        "        procps \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
    )
    missing = _contract_toolbelt_apt_packages() - _apt_installed_packages(
        stripped_dockerfile
    )
    assert missing == {"jq"}
