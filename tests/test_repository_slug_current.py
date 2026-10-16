"""Regression: the production overlay names the repository's public namespace.

The public repository lives at ``thomas-sodon/maestro-agent``. These tests load
the committed ``maestro.toml`` + ``maestro.production.toml`` through the same
``build_config_via_pydantic`` path the runtime uses and pin the values that
point the loop's PR traffic and publications at that namespace, so an overlay
edit cannot quietly retarget them.
"""

from __future__ import annotations

from pathlib import Path

from maestro.config_settings import build_config_via_pydantic

#: Project root — the parent of ``tests/``, and the ``config_dir`` argument
#: ``build_config_via_pydantic`` resolves ``maestro.toml`` and the
#: ``maestro.{env}.toml`` overlay under (mirrors ``test_config_flag_lifecycle``).
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The public namespace: the repository owner and the namespace the publication
#: pipeline creates published repos under (``[publication] owner``).
_OWNER = "thomas-sodon"

#: The public slug the production overlay must name.
_CURRENT_SLUG = f"{_OWNER}/maestro-agent"


def test_production_overlay_targets_current_repo() -> None:
    """The production overlay must resolve to the current repository slug.

    A typo'd or emptied ``[github] repo`` would point the loop nowhere. Parsing
    the committed ``maestro.toml`` + ``maestro.production.toml`` through the
    same ``build_config_via_pydantic`` load path the runtime uses pins the live
    value the loop actually points its PR traffic at.
    """
    config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    assert config.github_repo == _CURRENT_SLUG, (
        "maestro.production.toml's [github] repo resolves to "
        f"{config.github_repo!r}, but the repository lives at {_CURRENT_SLUG!r}; "
        "the loop would point its PR traffic at the wrong (or no) repository."
    )


def test_publication_owner_targets_public_namespace() -> None:
    """Publications are created under the configured public namespace.

    ``[publication] owner`` decides this: when it is empty,
    :meth:`SubprocessCodeHost.resolve_owner` falls back to ``gh api user``.
    Pinning the resolved value through the real load path keeps an overlay edit
    from quietly moving publications to another namespace.
    """
    config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    assert config.publication_owner == _OWNER, (
        f"[publication] owner resolves to {config.publication_owner!r}, but "
        f"publications belong to the {_OWNER!r} namespace."
    )
