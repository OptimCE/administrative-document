"""Regulator code → Region, the choice that selects a dossier's whole rulebook.

``domain/regions.py`` reads the SHARED ``reference/regulators.json``, which lives
at the monorepo root and is therefore absent from a standalone checkout. The
suite points ``REGULATORS_CONFIG_PATH`` at ``tests/fixtures/regulators.json``
(see ``tests/conftest.py``); these tests lock the mapping that fixture encodes,
and fail if the vendored copy ever drifts from the file the other services read.
"""

import json
from pathlib import Path

import pytest

from core.config import settings
from domain.regions import _DEFAULT_CONFIG_PATH, region_for_regulator, reset_cache
from shared.const import Region

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "regulators.json"


class TestRegionForRegulator:
    def test_an_active_regulator_maps_to_its_region(self):
        assert region_for_regulator("BE-WAL-CWAPE") is Region.WAL

    @pytest.mark.parametrize("code", ["BE-BRU-BRUGEL", "BE-VLA-VREG"])
    def test_an_inactive_regulator_has_no_region(self, code):
        # Declared but inactive: such a community has no supported paperwork, and
        # None is what makes the caller refuse it instead of quietly inventing a
        # Walloon deadline schedule.
        assert region_for_regulator(code) is None

    def test_an_unknown_code_has_no_region(self):
        assert region_for_regulator("BE-XXX-NOPE") is None

    @pytest.mark.parametrize("code", [None, ""])
    def test_no_regulator_at_all_is_not_an_error(self, code):
        assert region_for_regulator(code) is None


class TestRegistryResolution:
    """The loader itself — the paths that decide whether the service can start.

    Both tests repoint the registry, so each drops the module-global memoised map
    on the way out: leaving a bad path cached would break every later test.
    """

    def test_a_missing_registry_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "REGULATORS_CONFIG_PATH", str(tmp_path / "absent.json"))
        reset_cache()
        try:
            with pytest.raises(RuntimeError, match="regulators registry not found"):
                region_for_regulator("BE-WAL-CWAPE")
        finally:
            reset_cache()

    def test_an_unparseable_registry_fails_loudly(self, monkeypatch, tmp_path):
        broken = tmp_path / "regulators.json"
        broken.write_text("[{'code': not json}]", encoding="utf-8")
        monkeypatch.setattr(settings, "REGULATORS_CONFIG_PATH", str(broken))
        reset_cache()
        try:
            with pytest.raises(RuntimeError, match="is not valid JSON"):
                region_for_regulator("BE-WAL-CWAPE")
        finally:
            reset_cache()


class TestVendoredFixtureParity:
    def test_the_fixture_matches_the_shared_registry(self):
        """The fixture is a COPY; this is what keeps it honest.

        Skipped in a standalone checkout, where the shared file the copy is made
        from is simply not there — which is the whole reason the copy exists.
        """
        if not _DEFAULT_CONFIG_PATH.exists():
            pytest.skip(f"shared registry not checked out at {_DEFAULT_CONFIG_PATH}")

        canonical = json.loads(_DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        vendored = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        assert vendored == canonical, (
            f"{FIXTURE_PATH} has drifted from the shared {_DEFAULT_CONFIG_PATH}; "
            f"copy the shared file over it (billing and crm-backend vendor the same one)"
        )
