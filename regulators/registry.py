"""The registered regulator plugins, keyed by ``community.regulator``.

Explicit on purpose: a plugin is supported because it is listed here, not
because a module happens to exist. The entry points (``main.py``,
``worker/main.py``) hand ``REGISTRY`` to the engine; a community whose regulator
is absent gets no procedures ("regulator_not_supported").

Building it loads and validates every plugin's catalogue and calendar, so a
broken file stops the process at import, before it serves anything.
"""

from __future__ import annotations

from engine.contracts import PluginRegistry, build_registry
from regulators.cwape.plugin import CwapePlugin

REGISTRY: PluginRegistry = build_registry([CwapePlugin()])
