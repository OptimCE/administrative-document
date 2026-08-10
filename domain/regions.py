"""Resolve a community's regulator code to the Region its paperwork follows.

The canonical code list is the SHARED ``reference/regulators.json`` at the
monorepo root (the same file crm-backend and billing read) — there is no
vendored copy and no shared package, by design: one file, many readers.

Region is what selects the deadline rules and templates that apply, so an
unknown or inactive regulator must fail loudly rather than silently fall back
to Wallonia.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from core.config import settings
from shared.const import Region

logger = logging.getLogger(__name__)

# regulators.json describes the geography as a human-readable name; this is the
# only place that vocabulary is translated into our Region enum.
_REGION_BY_GEOGRAPHY: dict[str, Region] = {
    "Wallonia": Region.WAL,
    "Brussels": Region.BRU,
    "Flanders": Region.VLA,
}

# Dev fallback: the monorepo checkout keeps the shared file one level up. In a
# container REGULATORS_CONFIG_PATH is set explicitly and this is never used.
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "reference" / "regulators.json"


def _config_path() -> Path:
    configured = settings.REGULATORS_CONFIG_PATH.strip()
    return Path(configured) if configured else _DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def _regulator_regions() -> dict[str, Region]:
    """Load and memoise ``{regulator_code: Region}`` for the ACTIVE regulators.

    Inactive entries are deliberately excluded: a community carrying an inactive
    regulator has no supported paperwork, and we would rather 422 than invent a
    deadline schedule for it.
    """
    path = _config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            f"regulators registry not found at {path}. Set REGULATORS_CONFIG_PATH to the "
            f"shared reference/regulators.json (required in containers)."
        ) from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"regulators registry at {path} is not valid JSON: {exc}") from exc

    mapping: dict[str, Region] = {}
    for entry in raw:
        if not entry.get("active"):
            continue
        code = entry.get("code")
        region = _REGION_BY_GEOGRAPHY.get(entry.get("region", ""))
        if code and region is not None:
            mapping[code] = region
        elif code:
            logger.warning(
                "regulator %s has geography %r with no Region mapping; ignoring",
                code,
                entry.get("region"),
            )
    return mapping


def region_for_regulator(code: str | None) -> Region | None:
    """Return the Region for a regulator code, or None if unknown/inactive."""
    if not code:
        return None
    return _regulator_regions().get(code)


def reset_cache() -> None:
    """Drop the memoised registry. For tests that point at a fixture file."""
    _regulator_regions.cache_clear()
