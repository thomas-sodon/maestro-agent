"""Regression: fix-inconsistencies.md's `config` category names a real CLAUDE.md section.

Issue #1591. The `config` drift category (§3) opens with a `Sources:` bullet
enumerating where configuration source-of-truth lives. One source was written
as "the TOML Configuration section of CLAUDE.md" — but CLAUDE.md has no heading
by that name. An agent executing `/fix-inconsistencies` and grepping CLAUDE.md
for "TOML Configuration" finds nothing, so the config category's source-of-truth
pointer dead-ends and the drift check silently loses one of its anchors.

This test guards three things:

* The dead reference `TOML Configuration section of CLAUDE.md` is gone.
* The `config` category's `Sources:` bullet references `CLI, configuration, and
  secrets` — the CLAUDE.md heading that actually documents TOML config, so a
  grep for the section name resolves.
* That heading actually exists in CLAUDE.md. The pointer and the heading share
  one constant here, so a rename on *either* side fails the test instead of
  silently re-introducing the #1591 dead reference.
"""

from __future__ import annotations

from pathlib import Path

_FIX_INCONSISTENCIES_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "fix-inconsistencies.md"
)

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

# The CLAUDE.md section that documents TOML config. Both the command-doc
# `Sources:` pointer and the CLAUDE.md heading are checked against this single
# constant, so a rename on either side breaks the test (issue #1591).
_TOML_CONFIG_SECTION = "CLI, configuration, and secrets"


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def _sources_bullet(section: str) -> str:
    bullets = [
        line for line in section.splitlines() if line.lstrip().startswith("- Sources:")
    ]
    assert len(bullets) == 1, (
        f"Expected exactly one `- Sources:` bullet in the config category, "
        f"found {len(bullets)}."
    )
    return bullets[0]


def test_config_category_names_a_real_claude_md_section() -> None:
    doc = _FIX_INCONSISTENCIES_DOC.read_text(encoding="utf-8")

    assert "TOML Configuration section of CLAUDE.md" not in doc, (
        "The `config` category must not point at a nonexistent "
        "`TOML Configuration` heading in CLAUDE.md — the source-of-truth "
        "pointer dead-ends when an agent greps for it (issue #1591)."
    )

    config_section = _section_between(doc, "### 3. `config`", "### 4.")
    sources = _sources_bullet(config_section)
    assert _TOML_CONFIG_SECTION in sources, (
        f"The `config` category's `Sources:` bullet must reference the "
        f"`## {_TOML_CONFIG_SECTION}` section of CLAUDE.md — a heading "
        f"that grep resolves. Bullet:\n{sources}"
    )


def test_referenced_claude_md_section_heading_exists() -> None:
    """The pointer only resolves if the CLAUDE.md heading it names still exists.

    Guards the other half of #1591: the command doc can name a section that a
    later CLAUDE.md rename deletes, re-opening the dead reference. Resolving the
    pointer against CLAUDE.md here fails the test on such a rename.
    """
    claude_md = _CLAUDE_MD.read_text(encoding="utf-8")

    heading = f"## {_TOML_CONFIG_SECTION}"
    assert any(line.strip() == heading for line in claude_md.splitlines()), (
        f"CLAUDE.md must contain a `{heading}` heading so the `config` "
        f"category's `Sources:` pointer resolves; a rename on either side "
        f"re-introduces the dead reference from issue #1591."
    )
