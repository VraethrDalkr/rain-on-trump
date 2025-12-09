"""
place_aliases.py
~~~~~~~~~~~~~~~~
Hand-curated map of *common* schedule location strings → coordinates.
Keys must be **lower-case**, ASCII, and already stripped of fancy dashes
or NB-spaces; `location_service` does the same sanitisation before lookup.
"""

PLACE_ALIASES: dict[str, dict[str, float | str]] = {
    # ─── White House campus (same coords) ──────────────────────────────
    "the white house": {"lat": 38.897676, "lon": -77.036529, "name": "The White House"},
    "oval office": {"lat": 38.897676, "lon": -77.036529, "name": "Oval Office, WH"},
    "roosevelt room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Roosevelt Room, WH",
    },
    "cabinet room": {"lat": 38.897676, "lon": -77.036529, "name": "Cabinet Room, WH"},
    "east room": {"lat": 38.897676, "lon": -77.036529, "name": "East Room, WH"},
    "press briefing room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "James S. Brady Briefing Room, WH",
    },
    "south lawn": {"lat": 38.897676, "lon": -77.036529, "name": "South Lawn, WH"},
    "south portico": {"lat": 38.897676, "lon": -77.036529, "name": "South Portico, WH"},
    "the ellipse": {"lat": 38.893758, "lon": -77.035278, "name": "The Ellipse, DC"},
    "private dining room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Private Dining Room, WH",
    },
    "state dining room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "State Dining Room, WH",
    },
    "blue room": {"lat": 38.897676, "lon": -77.036529, "name": "Blue Room, WH"},
    "red room": {"lat": 38.897676, "lon": -77.036529, "name": "Red Room, WH"},
    "cross hall": {"lat": 38.897676, "lon": -77.036529, "name": "Cross Hall, WH"},
    "diplomatic room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Diplomatic Room, WH",
    },
    "grand foyer": {"lat": 38.897676, "lon": -77.036529, "name": "Grand Foyer, WH"},
    "north portico": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "North Portico, WH",
    },
    "situation room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Situation Room, WH",
    },
    # ─── Implicit White House indicators (schedule summaries) ───────────
    # "In-Town Pool Call Time" = press pool must report to White House
    "in-town pool call time": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "The White House",
    },
    # ─── Eisenhower Executive Office Building ─────────────────────────
    "south court auditorium": {
        "lat": 38.897592,
        "lon": -77.038668,
        "name": "South Court Auditorium, EEOB",
    },
    # ─── DC landmarks without city context ────────────────────────────
    "blair house": {"lat": 38.8969, "lon": -77.0385, "name": "Blair House, DC"},
    "national cathedral": {
        "lat": 38.930176,
        "lon": -77.070503,
        "name": "Washington National Cathedral, DC",
    },
    "washington national cathedral": {
        "lat": 38.930176,
        "lon": -77.070503,
        "name": "Washington National Cathedral, DC",
    },
    "federal reserve": {
        "lat": 38.8890,
        "lon": -77.0408,
        "name": "Federal Reserve (Eccles Building), DC",
    },
    "department of justice": {
        "lat": 38.8932,
        "lon": -77.0250,
        "name": "DOJ Robert F. Kennedy Building, DC",
    },
    "u.s. department of state": {
        "lat": 38.894504,
        "lon": -77.048475,
        "name": "U.S. Department of State, DC",
    },
    "department of state": {
        "lat": 38.894504,
        "lon": -77.048475,
        "name": "U.S. Department of State, DC",
    },
    "st. john's episcopal church": {
        "lat": 38.900410,
        "lon": -77.036106,
        "name": "St. John's Church, Lafayette Square, DC",
    },
    "st. john's church": {
        "lat": 38.900410,
        "lon": -77.036106,
        "name": "St. John's Church, Lafayette Square, DC",
    },
    "the people's house": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "The White House",
    },
    "rose garden": {"lat": 38.8975, "lon": -77.0371, "name": "Rose Garden, WH"},
    "north lawn": {"lat": 38.8982, "lon": -77.0355, "name": "North Lawn, WH"},
    # ─── DC landmarks that Nominatim misresolves ─────────────────────
    "u.s. naval observatory": {
        "lat": 38.9217,
        "lon": -77.0669,
        "name": "U.S. Naval Observatory (VP Residence), DC",
    },
    "mount vernon": {
        "lat": 38.7102,
        "lon": -77.0888,
        "name": "George Washington's Mount Vernon, VA",
    },
    # ─── Presidential retreat ─────────────────────────────────────────
    "camp david": {"lat": 39.6481, "lon": -77.4650, "name": "Camp David, MD"},
    # ─── Airports / transport ──────────────────────────────────────────
    "joint base andrews": {
        "lat": 38.810830,
        "lon": -76.866940,
        "name": "Joint Base Andrews, MD",
    },
    "morristown municipal airport": {
        "lat": 40.79935,
        "lon": -74.41487,
        "name": "Morristown Municipal Airport, NJ",
    },
    "palm beach intl airport": {
        "lat": 26.68390,
        "lon": -80.09559,
        "name": "Palm Beach Intl. Airport",
    },
    # ─── Trump residences & clubs ──────────────────────────────────────
    "mar-a-lago": {"lat": 26.675800, "lon": -80.036400, "name": "Mar-a-Lago, FL"},
    "trump tower": {"lat": 40.762500, "lon": -73.973000, "name": "Trump Tower, NYC"},
    "trump national golf club bedminster": {
        "lat": 40.645560,
        "lon": -74.639170,
        "name": "Trump Nat’l Golf Club Bedminster, NJ",
    },
    "trump national golf club washington dc": {
        "lat": 39.053000,
        "lon": -77.347000,
        "name": "Trump Nat’l Golf Club Washington DC, VA",
    },
    "trump national golf club doral": {
        "lat": 25.819550,
        "lon": -80.330970,
        "name": "Trump National Doral, FL",
    },
    "trump national doral miami": {
        "lat": 25.819550,
        "lon": -80.330970,
        "name": "Trump National Doral, FL",
    },
    "trump international hotel las vegas": {
        "lat": 36.129545,
        "lon": -115.172821,
        "name": "Trump International Hotel, Las Vegas",
    },
    "trump international golf links": {
        "lat": 57.27393,
        "lon": -2.03299,
        "name": "Trump International Golf Links, Aberdeen, Scotland",
    },
    "trump turnberry": {
        "lat": 55.3272,
        "lon": -4.8364,
        "name": "Trump Turnberry, Scotland",
    },
    "trump national golf club jupiter": {
        "lat": 26.890084,
        "lon": -80.089967,
        "name": "Trump Nat'l Golf Club Jupiter, FL",
    },
    "trump international golf club west palm beach": {
        "lat": 26.706,
        "lon": -80.036,
        "name": "Trump International Golf Club, West Palm Beach, FL",
    },
    "trump international golf club": {
        "lat": 26.706,
        "lon": -80.036,
        "name": "Trump International Golf Club, West Palm Beach, FL",
    },
    # ─── Courthouses that appear in 2024-25 court schedules ────────────
    "60 centre st": {
        "lat": 40.713460,
        "lon": -74.003100,
        "name": "NY County Supreme Court, 60 Centre St",
    },
    "wilkie d. ferguson jr. courthouse": {
        "lat": 25.774180,
        "lon": -80.194545,
        "name": "Ferguson U.S. Courthouse, Miami",
    },
    # ─── International one-offs seen in the 2024 calendar ──────────────
    "ritz-carlton abu dhabi": {
        "lat": 24.467970,
        "lon": 54.371600,
        "name": "Ritz-Carlton Abu Dhabi",
    },
    "dover air force base": {
        "lat": 39.129540,
        "lon": -75.466490,
        "name": "Dover AFB, DE",
    },
}
