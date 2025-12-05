"""
tests/test_fleet.py
~~~~~~~~~~~~~~~~~~~
Unit tests for the *fleet* definition.

Ensures we track exactly the call-signs we expect (VC-25As, C-32A, N757AF)
and that each entry carries the mandatory `icao` key.
"""

from __future__ import annotations

from app import fleet


def test_fleet_contents() -> None:
    """
    `FLEET` must contain exactly these call-signs when Trump is President:
      - "92-9000" (VC-25A)
      - "82-8000" (VC-25A spare)
      - "98-0001" (C-32A backup)
      - "N757AF"  (Trump Force One)
    """
    expected = {"92-9000", "82-8000", "98-0001", "N757AF"}
    assert set(fleet.FLEET) == expected


def test_fleet_has_icao_codes() -> None:
    """Every fleet entry MUST expose a valid 24-bit ICAO hex code."""
    for callsign, meta in fleet.FLEET.items():
        icao = meta.get("icao")
        assert isinstance(icao, str) and len(icao) == 6, f"Bad ICAO: {icao}"
