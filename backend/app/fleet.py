"""
fleet.py
~~~~~~~~
Single source of truth for every aircraft *Is It Raining on Trump?* tracks.

Key points
----------
* • When Donald Trump is **President**, the call-sign “Air Force One” is
    whichever USAF jet the President is aboard – normally the VC-25A pair
    or, for short runways, a C-32A.
* • For completeness we still track *Trump Force One* (N757AF).  The
    schedule occasionally shows private fundraising trips where the VC-25A
    isn’t used.

Each entry carries:
    ``icao`` : six-digit Mode-S hex code
    ``role`` : plain string descriptor (“vc25a”, “c32a”, “private”)
"""

from __future__ import annotations

from typing import Final

FLEET: Final[dict[str, dict[str, str]]] = {
    # ───────────── VC-25A jumbo pair (“Air Force One” most of the time)
    "92-9000": {"icao": "ae4e11", "role": "vc25a"},  # callsigns RCH476/RCH477 etc.
    "82-8000": {"icao": "ae4d8a", "role": "vc25a"},
    # ───────────── C-32A narrow-body (falls back when runways are short)
    "98-0001": {"icao": "ae6053", "role": "c32a"},
    # ───────────── Trump Force One – private 757-200
    "N757AF": {"icao": "aa3410", "role": "private"},
    # ───────────── Optional helicopter (commented – usually ADS-B-blocked)
    # "N7TP":    {"icao": "a0914f", "role": "helicopter"},
}

__all__ = ["FLEET"]
