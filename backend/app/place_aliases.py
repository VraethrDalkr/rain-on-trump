"""
place_aliases.py
~~~~~~~~~~~~~~~~
Hand-curated map of *common* schedule location strings → coordinates.
Keys must be **lower-case**, ASCII, and already stripped of fancy dashes
or NB-spaces; `location_service` does the same sanitisation before lookup.

`find_alias` matches the longest key contained in the text; failing that, a
conservative fuzzy match (difflib ratio ≥ :data:`FUZZY_MIN_RATIO` against the
whole string) absorbs feed typos such as "Joint Base Andreews".
"""

from __future__ import annotations

import difflib
import functools

FUZZY_MIN_RATIO = 0.9

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
    # ─── Frequent calendar venues (2025-26 feed, added 2026-11) ────────
    "palm beach international airport": {
        "lat": 26.6832,
        "lon": -80.0956,
        "name": "Palm Beach Intl. Airport",
    },
    "president donald j. trump international airport": {
        "lat": 26.6832,
        "lon": -80.0956,
        "name": "Palm Beach Intl. Airport (Pres. Donald J. Trump Intl.)",
    },
    "miami international airport": {
        "lat": 25.7959,
        "lon": -80.287,
        "name": "Miami Intl. Airport, FL",
    },
    "john f. kennedy center for the performing arts": {
        "lat": 38.8957,
        "lon": -77.0557,
        "name": "Kennedy Center, DC",
    },
    "u.s. capitol": {"lat": 38.8899, "lon": -77.0091, "name": "U.S. Capitol, DC"},
    "united nations": {
        "lat": 40.7489,
        "lon": -73.968,
        "name": "United Nations HQ, NYC",
    },
    "trump international hotel, las vegas": {
        "lat": 36.129545,
        "lon": -115.172821,
        "name": "Trump International Hotel, Las Vegas",
    },
    "evian resort": {
        "lat": 46.401,
        "lon": 6.59,
        "name": "Evian Resort, Évian-les-Bains, France",
    },
    "downtown manhattan heliport": {
        "lat": 40.7013,
        "lon": -74.009,
        "name": "Downtown Manhattan Heliport, NYC",
    },
    "davos congress centre": {
        "lat": 46.8003,
        "lon": 9.8263,
        "name": "Davos Congress Centre, Switzerland",
    },
    "united states institute of peace": {
        "lat": 38.8935,
        "lon": -77.049,
        "name": "U.S. Institute of Peace, DC",
    },
    "washington hilton": {
        "lat": 38.9165,
        "lon": -77.0457,
        "name": "Washington Hilton, DC",
    },
    "john f. kennedy international airport": {
        "lat": 40.6413,
        "lon": -73.7781,
        "name": "JFK Intl. Airport, NY",
    },
    "harry reid international airport": {
        "lat": 36.084,
        "lon": -115.1537,
        "name": "Harry Reid Intl. Airport, Las Vegas",
    },
    "arlington national cemetery": {
        "lat": 38.8783,
        "lon": -77.0687,
        "name": "Arlington National Cemetery, VA",
    },
    "windsor castle": {
        "lat": 51.4839,
        "lon": -0.6044,
        "name": "Windsor Castle, England",
    },
    "joint base elmendorf-richardson": {
        "lat": 61.251,
        "lon": -149.8065,
        "name": "JB Elmendorf-Richardson, AK",
    },
    "dallas fort worth international airport": {
        "lat": 32.8998,
        "lon": -97.0403,
        "name": "DFW Intl. Airport, TX",
    },
    "national mall": {"lat": 38.8895, "lon": -77.023, "name": "National Mall, DC"},
    "corpus christi international airport": {
        "lat": 27.7704,
        "lon": -97.5012,
        "name": "Corpus Christi Intl. Airport, TX",
    },
    "andrew w. mellon auditorium": {
        "lat": 38.893,
        "lon": -77.026,
        "name": "Andrew W. Mellon Auditorium, DC",
    },
    "the pentagon": {"lat": 38.8719, "lon": -77.0563, "name": "The Pentagon, VA"},
    "newark liberty international airport": {
        "lat": 40.6895,
        "lon": -74.1745,
        "name": "Newark Liberty Intl. Airport, NJ",
    },
    "walter reed national military medical center": {
        "lat": 39.0021,
        "lon": -77.095,
        "name": "Walter Reed NMMC, Bethesda, MD",
    },
    "dobbins air reserve base": {
        "lat": 33.9154,
        "lon": -84.5163,
        "name": "Dobbins ARB, GA",
    },
    "kaseya center": {"lat": 25.7814, "lon": -80.187, "name": "Kaseya Center, Miami"},
    "world forum, the hague": {
        "lat": 52.0896,
        "lon": 4.2833,
        "name": "World Forum, The Hague",
    },
    # ─── Verified additions (2026-11 alias proposal; source per entry) ───
    # existing alias 'the white house'
    "stakeout location": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "The White House (West Wing stakeout)",
    },
    # FAA ARP 39-02-55.8N 84-40-04.2W (airport-data.com via web search)
    "cincinnati/northern kentucky international airport": {
        "lat": 39.0488,
        "lon": -84.6678,
        "name": "Cincinnati/N. Kentucky Intl. Airport (CVG)",
    },
    # Wikipedia infobox coordinates 40.75806,-73.97500
    "lotte new york palace hotel": {
        "lat": 40.758,
        "lon": -73.975,
        "name": "Lotte New York Palace Hotel, NYC",
    },
    # Wikipedia infobox coordinates 32.79056,-96.81028
    "american airlines center, dallas": {
        "lat": 32.7906,
        "lon": -96.8103,
        "name": "American Airlines Center, Dallas",
    },
    # production geocode log (Daytona Beach International Airport)
    "daytona beach international airport": {
        "lat": 29.181644,
        "lon": -81.054299,
        "name": "Daytona Beach Intl. Airport, FL",
    },
    # Wikipedia infobox coordinates 42.60833,-82.83556
    "selfridge air national guard base, mi": {
        "lat": 42.6083,
        "lon": -82.8356,
        "name": "Selfridge ANGB, MI",
    },
    # KSKF 29.3842,-98.5811 (myflight.life, learn-atc, shipsgo)
    "kelly field, san antonio, tx": {
        "lat": 29.3842,
        "lon": -98.5811,
        "name": "Kelly Field Annex, JBSA, TX",
    },
    # Wikipedia infobox coordinates 52°21'54"N 0°28'51"E
    "raf mildenhall, england, united kingdom": {
        "lat": 52.365,
        "lon": 0.4808,
        "name": "RAF Mildenhall, England",
    },
    # planemapper 35.1709,-79.0145
    "pope field, fort bragg, nc": {
        "lat": 35.1709,
        "lon": -79.0145,
        "name": "Pope Field, Fort Bragg, NC",
    },
    # Wikipedia infobox coordinates 41.53389,-93.66306
    "des moines international airport": {
        "lat": 41.534,
        "lon": -93.6631,
        "name": "Des Moines Intl. Airport, IA",
    },
    # Wikipedia infobox coordinates 38.8975,-77.018111
    "national building museum": {
        "lat": 38.8975,
        "lon": -77.0181,
        "name": "National Building Museum, DC",
    },
    # production geocode log (Charlotte-Douglas International Airport)
    "charlotte douglas international airport": {
        "lat": 35.212978,
        "lon": -80.945656,
        "name": "Charlotte Douglas Intl. Airport, NC",
    },
    # FAA ARP 35-02-32.681N 89-58-36.045W (AirNav/FAA AIP via web search)
    "frederick w. smith international airport, memphis, tn": {
        "lat": 35.0424,
        "lon": -89.9767,
        "name": "Memphis Intl. Airport, TN",
    },
    # FAA ARP 33-56-32.987N 118-24-28.975W (AirNav)
    "los angeles international airport": {
        "lat": 33.9425,
        "lon": -118.4081,
        "name": "Los Angeles Intl. Airport, CA",
    },
    # Wikipedia infobox coordinates 40.813528,-74.074361
    "metlife stadium, east rutherford, nj": {
        "lat": 40.8135,
        "lon": -74.0744,
        "name": "MetLife Stadium, NJ",
    },
    # airport ARP 25.1173,51.3150 (metarcentral/pilotnav)
    "al udeid air base, qatar": {
        "lat": 25.1173,
        "lon": 51.315,
        "name": "Al Udeid Air Base, Qatar",
    },
    # FAA data 40-39-08.506N 75-26-25.461W (airportguide/airport-data)
    "lehigh valley international airport, allentown, pa": {
        "lat": 40.6524,
        "lon": -75.4404,
        "name": "Lehigh Valley Intl. Airport, PA",
    },
    # Wikipedia mirror 33.729622,-118.349576 (Rancho Palos Verdes)
    "trump national golf club los angeles": {
        "lat": 33.7296,
        "lon": -118.3496,
        "name": "Trump Nat'l Golf Club Los Angeles, CA",
    },
    # Wikipedia infobox coordinates 39°54'12"N 116°23'15"E
    "great hall of the people, beijing": {
        "lat": 39.9033,
        "lon": 116.3875,
        "name": "Great Hall of the People, Beijing",
    },
    # existing alias 'ritz-carlton abu dhabi' (comma variant not matched by
    "the ritz-carlton, abu dhabi, united arab emirates": {
        "lat": 24.46797,
        "lon": 54.3716,
        "name": "Ritz-Carlton Abu Dhabi",
    },
    # production geocode log (Reflecting Pool, Ward 2, Washington)
    "lincoln memorial reflecting pool": {
        "lat": 38.889347,
        "lon": -77.044665,
        "name": "Lincoln Memorial Reflecting Pool, DC",
    },
    # production geocode log (Crossroads Arena, 2800 S Harper Rd, Corinth)
    "crossroads arena, corinth, ms": {
        "lat": 34.911687,
        "lon": -88.54777,
        "name": "Crossroads Arena, Corinth, MS",
    },
    # Irish AIP ARP 524207N 0085529W
    "shannon airport, ireland": {
        "lat": 52.702,
        "lon": -8.9248,
        "name": "Shannon Airport, Ireland",
    },
    # Wikipedia 38°53'50"N 77°00'23"W
    "union station, washington": {
        "lat": 38.8971,
        "lon": -77.0063,
        "name": "Washington Union Station, DC",
    },
    # FAA ARP 40-46-38.072N 73-52-21.380W (AirNav)
    "laguardia airport": {
        "lat": 40.7772,
        "lon": -73.8726,
        "name": "LaGuardia Airport, NY",
    },
    # Wikipedia infobox coordinates 38.89806,-77.02083
    "capitol one arena": {
        "lat": 38.8981,
        "lon": -77.021,
        "name": "Capital One Arena, DC",
    },
    # Wikipedia infobox coordinates Old Post Office 38.89417,-77.02750
    "waldorf astoria washington dc": {
        "lat": 38.8942,
        "lon": -77.0275,
        "name": "Waldorf Astoria Washington DC (Old Post Office)",
    },
    # Wikipedia infobox coordinates 40.07250,116.59750
    "beijing capital international airport": {
        "lat": 40.0725,
        "lon": 116.5975,
        "name": "Beijing Capital Intl. Airport",
    },
    # FAA data via SkyVector N39°42.51' W77°43.59'
    "hagerstown regional airport": {
        "lat": 39.7085,
        "lon": -77.7265,
        "name": "Hagerstown Regional Airport, MD",
    },
    # Korean eAIP ARP 351050N 1285617E
    "gimhae international airport, busan, south korea": {
        "lat": 35.1806,
        "lon": 128.9381,
        "name": "Gimhae Intl. Airport, Busan",
    },
    # existing alias 'u.s. department of state'
    "state department": {
        "lat": 38.894504,
        "lon": -77.048475,
        "name": "U.S. Department of State, DC",
    },
    # existing alias 'the white house'
    "diplomatic reception room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Diplomatic Reception Room, WH",
    },
    # existing alias 'the white house'
    "state floor": {"lat": 38.897676, "lon": -77.036529, "name": "State Floor, WH"},
    # airport ARP 38.9445,-77.4558 (web search)
    "washington dulles international airport": {
        "lat": 38.9445,
        "lon": -77.4558,
        "name": "Washington Dulles Intl. Airport, VA",
    },
    # existing alias
    "indian treaty room": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Indian Treaty Room, EEOB (WH complex)",
    },
    # existing alias 'the white house'
    "palm room": {"lat": 38.897676, "lon": -77.036529, "name": "Palm Room, WH"},
    # existing alias 'the white house'
    "executive residence": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "Executive Residence, WH",
    },
    # existing alias 'the white house'
    "white house tennis court": {
        "lat": 38.897676,
        "lon": -77.036529,
        "name": "White House Tennis Court",
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


@functools.lru_cache(maxsize=2048)
def _fuzzy_key(text: str) -> str | None:
    best, best_ratio = None, FUZZY_MIN_RATIO
    for key in PLACE_ALIASES:
        ratio = difflib.SequenceMatcher(None, text, key).ratio()
        if ratio >= best_ratio:
            best, best_ratio = key, ratio
    return best


def find_alias(text: str) -> dict[str, float | str] | None:
    """Return the alias whose key occurs in *text* (longest key wins).

    Without a substring match, fall back to the key most similar to the whole
    *text* (ratio ≥ :data:`FUZZY_MIN_RATIO`), or None.
    """
    best: str | None = None
    for key in PLACE_ALIASES:
        if key in text and (best is None or len(key) > len(best)):
            best = key
    if best is None and text:
        best = _fuzzy_key(text)
    return PLACE_ALIASES[best] if best is not None else None
