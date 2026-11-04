"""
tests/test_fleet.py
~~~~~~~~~~~~~~~~~~~
Unit tests for the *fleet* definition and the Air Force One callsign gate.
"""

from __future__ import annotations

import re

from app import fleet

# Verified 2026-11-04 against tar1090-db / hexdb.io
EXPECTED_HEX = {
    "82-8000": "adfdf8",
    "92-9000": "adfdf9",
    "25-3300": "af83f3",
    "N7478D": "aa0fff",
    "98-0001": "adfeb7",
    "98-0002": "adfeb8",
    "99-0003": "adfeb9",
    "99-0004": "adfeba",
    "09-0015": "ae4ae6",
    "09-0016": "ae4ae8",
    "09-0017": "ae4aea",
    "N757AF": "aa3410",
}


def test_fleet_contents() -> None:
    assert {t: m["icao"] for t, m in fleet.FLEET.items()} == EXPECTED_HEX


def test_fleet_has_valid_icao_codes() -> None:
    for tail, meta in fleet.FLEET.items():
        assert re.fullmatch(r"[0-9a-f]{6}", meta["icao"]), tail
    assert len(fleet.TAIL_BY_ICAO) == len(fleet.FLEET), "duplicate hex codes"


def test_only_c32a_require_af1_callsign() -> None:
    gated = {t for t, m in fleet.FLEET.items() if m["require_callsign"]}
    assert gated == {t for t, m in fleet.FLEET.items() if m["role"] == "c32a"}


def test_counts_as_president() -> None:
    assert fleet.counts_as_president("92-9000", "")
    assert fleet.counts_as_president("92-9000", "SAM29000")
    assert fleet.counts_as_president("N757AF", None)
    assert fleet.counts_as_president("98-0001", "AF1     ")
    assert fleet.counts_as_president("98-0001", "af1")
    assert not fleet.counts_as_president("98-0001", "SAM0001")
    assert not fleet.counts_as_president("98-0001", "AF2")
    assert not fleet.counts_as_president("98-0001", "")
    assert not fleet.counts_as_president("11-5738", "AF1")
