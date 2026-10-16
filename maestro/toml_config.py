"""TOML file reading and per-environment overlay merge.

``maestro.toml`` is committed to the repo and defines all default values.
Environment overlays (``maestro.{env}.toml``) are also committed and override
specific settings for each deployment target (e.g. ``maestro.production.toml``).

This module is the focused "read + merge" substrate the declarative parser in
:mod:`maestro.config_settings` calls into: :func:`_read_toml` parses one file,
:func:`_deep_merge` layers the ``{env}`` overlay onto the base, and
:data:`_KNOWN_EXTERNAL_SECTIONS` names the top-level tables consumed by sibling
packages sharing ``maestro.toml``. The former hand-maintained
``(section, key) -> (field, type)`` mapping table and flatten/overlay loader have
been retired in favour of the declarative ``pydantic-settings`` models (proposal
106); the parse itself now lives entirely in ``config_settings.py``.
"""

from __future__ import annotations

import copy
import logging
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger("maestro")

# Top-level sections consumed by other packages sharing maestro.toml (today
# ``maestro_review``'s ``[review]`` section); allocated once at import and read
# by ``config_settings.build_config_via_pydantic``'s unknown-top-level-section
# guard.
_KNOWN_EXTERNAL_SECTIONS: frozenset[str] = frozenset({"review"})


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge *overlay* onto *base*, recursing into nested dicts."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    """Read and parse a single TOML file. Returns empty dict on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
        result: dict[str, Any] = tomllib.loads(raw)
        return result
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("Failed to read or parse TOML config at %s", path, exc_info=True)
        return {}
