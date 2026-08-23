"""Placing winning outlets on a map of Trinidad & Tobago.

The winner cards give free-text addresses ("#16 SOUTHERN MAIN ROAD, MARABELLA"),
never coordinates. Rather than depend on a geocoding service at page-load time,
addresses are matched offline against a gazetteer of Trinidad & Tobago towns,
villages and districts, and the result is cached in the database.

Coordinates are approximate town centres - good enough to show which part of the
country a ticket was sold in, which is all the source data supports.
"""
from __future__ import annotations

import re
import unicodedata

# name -> (lat, lon). Ordered longest-first at match time so that
# "SAN JUAN" is not swallowed by "SAN".
GAZETTEER = {
    # --- Port of Spain and the East-West Corridor -------------------------
    "PORT OF SPAIN": (10.6549, -61.5019),
    "POS": (10.6549, -61.5019),
    "WOODBROOK": (10.6603, -61.5240),
    "NEWTOWN": (10.6621, -61.5175),
    "ST CLAIR": (10.6683, -61.5222),
    "BELMONT": (10.6600, -61.4980),
    "LAVENTILLE": (10.6500, -61.4833),
    "MORVANT": (10.6560, -61.4740),
    "SAN JUAN": (10.6500, -61.4500),
    "BARATARIA": (10.6450, -61.4670),
    "ARANGUEZ": (10.6420, -61.4420),
    "ST JOSEPH": (10.6560, -61.4110),
    "CUREPE": (10.6420, -61.4030),
    "ST AUGUSTINE": (10.6420, -61.3990),
    "TUNAPUNA": (10.6520, -61.3900),
    "TACARIGUA": (10.6400, -61.3720),
    "TRINCITY": (10.6180, -61.3500),
    "AROUCA": (10.6250, -61.3350),
    "PIARCO": (10.5950, -61.3370),
    "D'ABADIE": (10.6270, -61.3110),
    "DABADIE": (10.6270, -61.3110),
    "ARIMA": (10.6370, -61.2830),
    "MALABAR": (10.6510, -61.2870),
    "MAUSICA": (10.6220, -61.3220),
    "CUNUPIA": (10.5560, -61.3830),
    "CHAGUANAS": (10.5150, -61.4110),
    "CHARLIEVILLE": (10.5290, -61.4020),
    "ENTERPRISE": (10.5090, -61.4340),
    "FELICITY": (10.5210, -61.4470),
    "MONTROSE": (10.5170, -61.4300),
    "LONGDENVILLE": (10.5220, -61.3770),
    "CARAPICHAIMA": (10.4780, -61.3990),
    "CHASE VILLAGE": (10.4940, -61.4180),
    "FREEPORT": (10.4500, -61.3830),
    "COUVA": (10.4210, -61.4560),
    "CALIFORNIA": (10.4060, -61.4530),
    "POINT LISAS": (10.4020, -61.4780),
    # --- West and north-west ---------------------------------------------
    "DIEGO MARTIN": (10.7150, -61.5480),
    "PETIT VALLEY": (10.7040, -61.5390),
    "CARENAGE": (10.6830, -61.5900),   # nudged inland, see SAN FERNANDO
    "GLENCOE": (10.6890, -61.5620),
    "WESTMOORINGS": (10.6740, -61.5560),
    "CHAGUARAMAS": (10.6810, -61.6390),
    "MARAVAL": (10.6900, -61.5210),
    "ST JAMES": (10.6690, -61.5350),
    "FEDERATION PARK": (10.6690, -61.5290),
    "COCORITE": (10.6710, -61.5490),
    "CASCADE": (10.6760, -61.5020),
    "ST ANN'S": (10.6810, -61.5060),
    "ST ANNS": (10.6810, -61.5060),
    # --- South ------------------------------------------------------------
    # nudged ~0.5 km inland: the town centre sits marginally outside the
    # coarse Natural Earth coastline used for the map
    "SAN FERNANDO": (10.2780, -61.4640),
    "MARABELLA": (10.3060, -61.4520),
    "GASPARILLO": (10.3230, -61.4230),
    "CLAXTON BAY": (10.3560, -61.4600),
    "PRINCES TOWN": (10.2720, -61.3720),
    "DEBE": (10.2010, -61.4520),
    "PENAL": (10.1670, -61.4670),
    "SIPARIA": (10.1420, -61.5060),
    "FYZABAD": (10.1830, -61.5500),
    "SANTA FLORA": (10.1120, -61.6320),
    "POINT FORTIN": (10.1750, -61.6800),
    "LA BREA": (10.2400, -61.6200),
    "VESSIGNY": (10.2130, -61.6470),
    "OROPOUCHE": (10.2170, -61.4330),
    "BARRACKPORE": (10.2170, -61.4000),
    "MORUGA": (10.0700, -61.2830),
    "RIO CLARO": (10.3060, -61.1750),
    "MAYARO": (10.2900, -61.0000),
    "GUAYAGUAYARE": (10.1420, -61.0330),
    "TABAQUITE": (10.3900, -61.2950),
    "FLANAGIN TOWN": (10.3830, -61.3500),
    "WILLIAMSVILLE": (10.2330, -61.3500),
    "NEW GRANT": (10.2130, -61.3210),
    "TARODALE": (10.2900, -61.4460),
    "PLEASANTVILLE": (10.2800, -61.4470),
    "VISTABELLA": (10.2700, -61.4600),
    "LA ROMAIN": (10.2510, -61.4620),
    "GOLCONDA": (10.2360, -61.4530),
    "DUNCAN VILLAGE": (10.2740, -61.4590),
    "CEDROS": (10.0870, -61.8000),
    "ERIN": (10.0770, -61.6600),
    "CHATHAM": (10.0870, -61.7500),
    "LOS IROS": (10.0730, -61.6600),
    "GRANVILLE": (10.0800, -61.7800),
    "ICACOS": (10.0700, -61.9200),
    "BUENOS AYRES": (10.1700, -61.5900),
    "PALO SECO": (10.1100, -61.6100),
    "GUAPO": (10.1620, -61.6500),
    # --- Central and east --------------------------------------------------
    "SANGRE GRANDE": (10.5850, -61.1310),
    "VALENCIA": (10.6500, -61.2000),
    "CUMUTO": (10.5670, -61.2170),
    "MANZANILLA": (10.5060, -61.0330),
    "TOCO": (10.8330, -60.9500),
    "MATELOT": (10.7900, -61.0500),
    "BLANCHISSEUSE": (10.7830, -61.3000),
    "BRASSO": (10.3900, -61.3100),
    "TALPARO": (10.4670, -61.2830),
    "WALLERFIELD": (10.6000, -61.2670),
    "GUAICO": (10.5900, -61.1000),
    "BICHE": (10.4330, -61.1170),
    "ECCLESVILLE": (10.3500, -61.1200),
    "PRINCES TOWN ROAD": (10.2720, -61.3720),
    "CARONI": (10.5830, -61.4000),
    "CUREPE JUNCTION": (10.6420, -61.4030),
    "EL DORADO": (10.6480, -61.3800),
    "PARADISE": (10.6350, -61.3600),
    "LOPINOT": (10.6830, -61.3170),
    "SANTA CRUZ": (10.7000, -61.4670),
    "SAN RAFAEL": (10.5330, -61.2670),
    "CHAGUANAS MAIN ROAD": (10.5150, -61.4110),
    "PRINCE TOWN": (10.2720, -61.3720),
    "SIPARIA OLD ROAD": (10.1420, -61.5060),
    # --- Tobago ------------------------------------------------------------
    "TOBAGO": (11.2500, -60.6800),
    "SCARBOROUGH": (11.1830, -60.7370),
    "CROWN POINT": (11.1520, -60.8320),
    "CANAAN": (11.1600, -60.8180),
    "BON ACCORD": (11.1580, -60.8300),
    "PLYMOUTH": (11.2170, -60.7830),
    "ROXBOROUGH": (11.2500, -60.5830),
    "CHARLOTTEVILLE": (11.3200, -60.5500),
    "SPEYSIDE": (11.3000, -60.5330),
    "MOUNT PLEASANT": (11.1700, -60.7800),
    "SIGNAL HILL": (11.1740, -60.7570),
    "SHERWOOD PARK": (11.1780, -60.7620),
    "BUCCOO": (11.1770, -60.8060),
    "LOWLANDS": (11.1620, -60.7900),
    "GLEN ROAD": (11.1830, -60.7370),
    # --- extra districts sometimes used --------------------------------------
    "MT LAMBERT": (10.6480, -61.4300),
    "MOUNT LAMBERT": (10.6480, -61.4300),
    "SUCCESS": (10.6520, -61.4790),
    "CHAMPS FLEURS": (10.6500, -61.4270),
    "MACOYA": (10.6350, -61.3860),
    "BON AIR": (10.6180, -61.3170),
    "LA HORQUETTA": (10.6100, -61.3060),
    "SAN SOUCI": (10.8300, -61.0200),
    "CUMANA": (10.8200, -61.0700),
    "GRANDE RIVIERE": (10.8330, -61.0500),
    "MADRAS": (10.6050, -61.2860),
    "CHIN CHIN": (10.5470, -61.3600),
    "PIPARO": (10.2830, -61.3170),
    "TUNAPUNA ROAD": (10.6520, -61.3900),
    "EASTERN MAIN ROAD": (10.6480, -61.4200),
    "SOUTHERN MAIN ROAD": (10.3800, -61.4400),
    "DIEGO MARTIN MAIN ROAD": (10.7150, -61.5480),
    "WESTERN MAIN ROAD": (10.6790, -61.5900),
    "CUNAPO SOUTHERN ROAD": (10.5850, -61.1310),
    "NARIVA": (10.3800, -61.0700),
}

# common abbreviations that appear inside addresses
_EXPANSIONS = [
    (r"\bE\.?\s?M\.?\s?R\.?\b", "EASTERN MAIN ROAD"),
    (r"\bS\.?\s?M\.?\s?R\.?\b", "SOUTHERN MAIN ROAD"),
    (r"\bW\.?\s?M\.?\s?R\.?\b", "WESTERN MAIN ROAD"),
    (r"\bP\.?\s?O\.?\s?S\.?\b", "PORT OF SPAIN"),
    (r"\bST\.\s", "ST "),
    (r"\bMT\.\s", "MT "),
    (r"\bS/GRANDE\b", "SANGRE GRANDE"),
    (r"\bSAN F'DO\b", "SAN FERNANDO"),
]

# longest names first so specific places beat the generic ones they contain
_ORDERED = sorted(GAZETTEER, key=len, reverse=True)


def normalize(text):
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.upper()
    for pat, rep in _EXPANSIONS:
        t = re.sub(pat, rep, t)
    t = t.replace("&", " AND ")
    t = re.sub(r"[^A-Z0-9' ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def match_place(*texts):
    """Find the most specific gazetteer place mentioned in any of `texts`.

    Later arguments are searched first, so the area field wins over the outlet
    name when both name a place.
    """
    for raw in reversed([t for t in texts if t]):
        hay = normalize(raw)
        if not hay:
            continue
        for name in _ORDERED:
            key = normalize(name)
            if not key:
                continue
            if re.search(r"(?<![A-Z0-9])" + re.escape(key) + r"(?![A-Z0-9])", hay):
                return name, GAZETTEER[name]
    return None, None


def geocode_rows(con, overwrite=False):
    """Fill lat/lon on winner_locations from the gazetteer. Returns (matched, total)."""
    where = "" if overwrite else " WHERE lat IS NULL"
    rows = con.execute(
        "SELECT winner_id, seq, outlet, address, area FROM winner_locations" + where
    ).fetchall()
    matched = 0
    for r in rows:
        name, coords = match_place(r["outlet"], r["address"], r["area"])
        if coords is None:
            con.execute("UPDATE winner_locations SET place=NULL, lat=NULL, lon=NULL, "
                        "geo_method='unmatched' WHERE winner_id=? AND seq=?",
                        (r["winner_id"], r["seq"]))
            continue
        con.execute(
            "UPDATE winner_locations SET place=?, lat=?, lon=?, geo_method='gazetteer' "
            "WHERE winner_id=? AND seq=?",
            (name, coords[0], coords[1], r["winner_id"], r["seq"]))
        matched += 1
    con.commit()
    return matched, len(rows)
