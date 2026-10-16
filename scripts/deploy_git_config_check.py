"""Refuse a deploy whose repo carries a git config key outside a fixed allowlist.

``scripts/safe-deploy.sh`` runs this as ``python3 -I`` on the host before its
first git command (proposal 150, Step 1). The loop container mounts the
checkout read-write, ``.git/`` included, so a planted ``.git/config`` key such
as ``core.fsmonitor``, ``include.path`` or ``filter.<driver>.smudge`` would run
on the host during fetch, reset, clean or diff. Every key outside
``DEPLOY_GIT_CONFIG_ALLOWLIST`` is refused, as is a non-empty
``.git/info/attributes`` and a ``.git/commondir`` or ``.git/config.worktree``
(both redirect what git reads). The reader fails closed: any line it cannot parse is
a refusal. Stdlib only, no syntax newer than Python 3.10.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEPLOY_GIT_CONFIG_ALLOWLIST = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "remote.origin.url",
        "remote.origin.fetch",
    }
)
_BRANCH_KEY_SUFFIXES = (".remote", ".merge")

# A plain git dir honours ``commondir`` (config and info/attributes are then read
# from the directory it names) and ``config.worktree`` (read when
# extensions.worktreeConfig is set); either would move the keys out of view.
_REDIRECT_FILES = ("commondir", "config.worktree")

_EXIT_PASS = 0
_EXIT_REFUSED = 3

_NAME = r"[A-Za-z0-9][A-Za-z0-9-]*"
_SECTION_RE = re.compile(r"\[\s*(" + _NAME + r"(?:\." + r"[^\]\s\"]+)?)\s*\](.*)$")
_SUBSECTION_RE = re.compile(r'\[\s*(' + _NAME + r')\s+"((?:[^"\\]|\\.)*)"\s*\](.*)$')
_KEY_RE = re.compile(r"(" + _NAME + r")\s*(?:=(.*))?$")


class ConfigRefused(ValueError):
    """The config text cannot be parsed safely; the deploy must refuse."""


def _strip_comment(text: str) -> str:
    """Drop a trailing ``#``/``;`` comment outside double quotes."""
    in_quote = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quote = not in_quote
        elif char in "#;" and not in_quote:
            return text[:index]
    return text


def _parse_header(line: str, lineno: int) -> tuple[str, str]:
    """Return ``(section_prefix, remainder)`` for a ``[...]`` header line."""
    sub = _SUBSECTION_RE.match(line)
    if sub is not None:
        name = sub.group(1).lower()
        subsection = re.sub(r"\\(.)", r"\1", sub.group(2))
        return f"{name}.{subsection}", sub.group(3)
    legacy = _SECTION_RE.match(line)
    if legacy is not None:
        head, _, rest = legacy.group(1).partition(".")
        prefix = head.lower() if not rest else f"{head.lower()}.{rest.lower()}"
        return prefix, legacy.group(2)
    raise ConfigRefused(f"line {lineno}: unparseable or unterminated section header")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_config_entries(text: str) -> list[tuple[str, str]]:
    """Return every ``(section[.subsection].key, value)`` pair in a git config text.

    Section and key names are lowercased; a subsection keeps its case; a value
    loses its comment and surrounding double quotes (a bare key reads ``""``).
    A continuation backslash, an unparseable line, or a key before any header
    raises ``ConfigRefused``.
    """
    entries: list[tuple[str, str]] = []
    section: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.rstrip().endswith("\\"):
            raise ConfigRefused(f"line {lineno}: continuation backslash")
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section, line = _parse_header(line, lineno)
            line = line.strip()
            if not line or line[0] in "#;":
                continue
        match = _KEY_RE.match(_strip_comment(line).strip())
        if match is None or section is None:
            raise ConfigRefused(f"line {lineno}: unparseable line")
        entries.append((f"{section}.{match.group(1).lower()}", _unquote(match.group(2) or "")))
    return entries


def _key_allowed(key: str) -> bool:
    if key in DEPLOY_GIT_CONFIG_ALLOWLIST:
        return True
    if not key.startswith("branch."):
        return False
    return any(
        key.endswith(suffix) and len(key) > len("branch.") + len(suffix) for suffix in _BRANCH_KEY_SUFFIXES
    )


def check_repo(repo: Path, expected_origin: str) -> list[str]:
    """Return the reasons ``repo`` must be refused; empty means it passes."""
    git_dir = repo / ".git"
    redirects = [git_dir / name for name in _REDIRECT_FILES if (git_dir / name).exists()]
    if redirects:
        return [f"{path} exists (it redirects where git reads config/attributes)" for path in redirects]
    config = git_dir / "config"
    if not config.exists():
        return [f"{config} is missing (a gitfile or non-repo is refused)"]
    text = config.read_text(encoding="utf-8")
    try:
        entries = parse_config_entries(text)
    except ConfigRefused as exc:
        return [f"{config}: {exc}"]
    reasons = [f"key outside the allowlist: {key}" for key, _ in entries if not _key_allowed(key)]
    urls = [value for key, value in entries if key == "remote.origin.url"]
    if not urls:
        reasons.append("remote.origin.url is missing")
    elif any(url != expected_origin for url in urls):
        reasons.append("remote.origin.url differs from the expected origin")
    attributes = repo / ".git" / "info" / "attributes"
    if attributes.exists() and attributes.read_text(encoding="utf-8").strip():
        reasons.append(f"{attributes} is non-empty")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--expected-origin", required=True)
    args = parser.parse_args(argv)
    try:
        reasons = check_repo(Path(args.repo), args.expected_origin)
    except (OSError, UnicodeDecodeError) as exc:
        reasons = [f"cannot read the repo config: {type(exc).__name__}"]
    for reason in reasons:
        print(f"deploy-git-config: {reason}", file=sys.stderr)
    return _EXIT_REFUSED if reasons else _EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
