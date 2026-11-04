"""
clock.py
~~~~~~~~
Single source of "now" for location decisions (fusion, flight history,
calendar windows), so scenario tests can freeze time by patching
:func:`now`. TTL caches keep their own wall-clock timing.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

UTC: Final = dt.timezone.utc


def now() -> dt.datetime:
    """Return the current time as an aware UTC datetime."""
    return dt.datetime.now(UTC)
