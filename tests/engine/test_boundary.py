"""The engine knows no regulator (spec, "Frontière vérifiée en CI").

``engine/`` must never import ``regulators``: the plugins import the engine,
the entry points hand it a registry. Three checks, because each misses what the
next one catches:

1. the AST of every engine module, for a written ``import regulators…``;
2. a fresh interpreter importing every engine module with ``regulators``
   refused from ``sys.meta_path``, for an import that arrives TRANSITIVELY
   (engine → some helper → regulators). It needs a subprocess: this pytest
   process has imported ``regulators`` already;
3. the text, for a regulator's name: "the engine knows none" is about strings
   too, a ``if code == "BE-WAL-CWAPE"`` is a dependency no import check sees.

No import-linter: these do the job without a new dependency.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess  # a fresh interpreter is the only way to get a clean sys.modules
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]
ENGINE = SERVICE_ROOT / "engine"
ENGINE_FILES = sorted(ENGINE.rglob("*.py"))
ENGINE_MODULES = sorted(
    ".".join(path.relative_to(SERVICE_ROOT).with_suffix("").parts).removesuffix(".__init__")
    for path in ENGINE_FILES
)

# The regulators OptimCE knows of (reference/regulators.json), by code and name.
_REGULATOR_NAMES = re.compile(r"CWAPE|BRUGEL|VREG|BE-WAL|BE-BRU|BE-VLA", re.IGNORECASE)


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def test_the_engine_has_modules_to_check():
    assert "engine.contracts" in ENGINE_MODULES
    assert "engine.catalogue_sync" in ENGINE_MODULES


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_engine_module_imports_a_plugin(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offending = [m for m in _imported_modules(tree) if m.partition(".")[0] == "regulators"]
    assert not offending, f"{path.name} imports {offending}: the engine must not know a plugin"


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_engine_module_names_a_regulator(path):
    found = sorted(set(_REGULATOR_NAMES.findall(path.read_text(encoding="utf-8"))))
    assert not found, f"{path.name} names {found}: regulator specifics belong in its plugin"


_PROBE = """
import importlib
import sys


class NoPlugins:
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] == "regulators":
            raise ImportError(f"the engine imported {name!r}")
        return None


sys.meta_path.insert(0, NoPlugins())
for module in MODULES:
    importlib.import_module(module)
assert not [m for m in sys.modules if m.partition(".")[0] == "regulators"]
"""


def test_the_engine_imports_with_every_plugin_absent():
    environment = os.environ.copy()
    environment.setdefault("ENV", "test")
    probe = subprocess.run(  # noqa: S603 — fixed argv, no shell, sys.executable
        [sys.executable, "-c", f"MODULES = {ENGINE_MODULES!r}\n{_PROBE}"],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_the_checks_would_see_an_import():
    """The AST reader is what the per-file test trusts; prove it reads both forms."""
    tree = ast.parse("import regulators.registry\nfrom regulators.testing import fake\n")
    assert _imported_modules(tree) == ["regulators.registry", "regulators.testing"]
