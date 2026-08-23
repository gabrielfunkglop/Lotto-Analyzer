"""Normalisation of the many date / time / money formats the sources use.

The NLCB site alone publishes draw dates in three different shapes
(``20250802``, ``08/19/2026`` US order, ``22/08/2026`` UK order) depending on the
game, so every parse is disambiguated against the draw's WordPress publish date
when one is available.
"""
from __future__ import annotations

import datetime as dt
import re

MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Period label -> canonical draw time (NLCB daily-game schedule).
PERIOD_TIMES = {
    "morning": "10:30",
    "midday": "13:00",
    "afternoon": "16:00",
    "evening": "19:00",
    "am": "10:30",
    "pm": "19:00",
}


def parse_archive_date(s):
    """`02-Jun-10` / `5-Jun-10` -> `2010-06-02`."""
    if not s:
        return None
    m = re.match(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{2,4})\s*$", s)
    if not m:
        return None
    day, mon, yr = int(m.group(1)), MONTHS.get(m.group(2).lower()), int(m.group(3))
    if mon is None:
        return None
    if yr < 100:
        yr += 2000 if yr <= 79 else 1900
    try:
        return dt.date(yr, mon, day).isoformat()
    except ValueError:
        return None


def parse_nlcb_date(s, hint=None):
    """Parse an NLCB ACF ``draw_date``.

    Handles ``YYYYMMDD``, ``MM/DD/YYYY`` and ``DD/MM/YYYY``. Ambiguous slash
    dates are resolved with ``hint`` (the WordPress publish date, ISO string):
    the reading closest to - and not after - the publish date wins.
    """
    if not s:
        return None
    s = str(s).strip()

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if not m:
        m2 = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", s)
        if m2:
            try:
                return dt.date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3))).isoformat()
            except ValueError:
                return None
        return None

    a, b, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    cands = []
    for mon, day, order in ((a, b, "MDY"), (b, a, "DMY")):
        try:
            cands.append((dt.date(yr, mon, day), order))
        except ValueError:
            pass
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][0].isoformat()

    if hint:
        try:
            h = dt.date.fromisoformat(str(hint)[:10])
        except ValueError:
            h = None
        if h:
            # prefer a date at or shortly before the publish date
            scored = sorted(cands, key=lambda c: (abs((h - c[0]).days), (c[0] > h)))
            return scored[0][0].isoformat()
    # no hint: US order is what the majority of NLCB CPTs use
    return cands[0][0].isoformat()


def parse_time(s):
    """`20:25:00`, `6:55 pm`, `8:30 PM` -> `HH:MM` 24h."""
    if not s:
        return None
    s = str(s).strip().lower()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(4)
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23):
        return None
    return f"{h:02d}:{mi:02d}"


def period_from_time(hhmm):
    if not hhmm:
        return None
    h = int(hhmm[:2])
    if h < 12:
        return "Morning"
    if h < 15:
        return "Midday"
    if h < 18:
        return "Afternoon"
    return "Evening"


def normalize_period(label):
    if not label:
        return None
    lab = label.strip().lower()
    if lab in PERIOD_TIMES:
        return lab.capitalize() if lab not in ("am", "pm") else ("Morning" if lab == "am" else "Evening")
    return label.strip().title() or None


def money_to_cents(s):
    """`$5,170,673.15` -> 517067315. Returns None for 'X', 'NA', blanks."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(round(float(s) * 100))
    t = str(s).strip()
    if not t or t.upper() in ("X", "NA", "N/A", "NO DATA", "-", "TBA"):
        return None
    t = t.replace("$", "").replace(",", "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(million|m|billion|b|k)?$", t, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("million", "m"):
        val *= 1_000_000
    elif unit in ("billion", "b"):
        val *= 1_000_000_000
    elif unit == "k":
        val *= 1_000
    return int(round(val * 100))


def dow(iso_date):
    if not iso_date:
        return None
    try:
        return dt.date.fromisoformat(iso_date).weekday()
    except ValueError:
        return None


def parse_int(s):
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.upper() in ("X", "NA", "N/A", "NO DATA", "-", ""):
        return None
    m = re.match(r"^-?\d+$", t)
    return int(t) if m else None
