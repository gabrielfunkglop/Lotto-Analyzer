"""Deep historical archive: nlcbplaywhelotto.com month-by-month result tables.

This is an unofficial mirror, but it is the only source with real depth - Play
Whe / Pick 2 / Cash Pot back to 2000, Lotto Plus to 2001 - and for the jackpot
games it publishes an explicit ``Wins`` column (number of jackpot winners) that
the official site does not expose.

Each game page renders a POST-driven month query. The result table's header row
is used to map columns, because the column set changed several times over the
25 years of history.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .. import normalize as nz

log = logging.getLogger(__name__)

BASE = "https://www.nlcbplaywhelotto.com"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# game -> (page slug, month field, year field, submit field, first year available)
PAGES = {
    "lotto":      ("nlcb-lotto-plus-results",  "lotto_month",   "lotto_year",   "month_year_btn", 2001),
    "playwhe":    ("nlcb-play-whe-results",    "playwhe_month", "playwhe_year", "month_year_btn", 2000),
    "pick2":      ("nlcb-pick-2-results",      "search_month",  "search_year",  "submit",         2000),
    "pick4":      ("nlcb-pick-4-results",      "search_month",  "search_year",  "submit",         2012),
    "cashpot":    ("nlcb-cashpot-results",     "search_month",  "search_year",  "submit",         2000),
    "winforlife": ("nlcb-win-for-life-results", "search_month", "search_year",  "submit",         2022),
}

SOURCE = "nlcbplaywhelotto"


def _norm_header(h):
    h = re.sub(r"\s+", " ", h or "").strip().lower()
    h = h.replace("draw#", "draw").replace("draw #", "draw")
    return h


def _split_numbers(text):
    """`3-13-14-31-34`, `4|5|7|9|19`, `2 17 19 20 23 24` -> [3, 13, ...]."""
    if not text:
        return []
    parts = [p for p in re.split(r"[|\-,/\s]+", text.strip()) if p != ""]
    out = []
    for p in parts:
        if re.fullmatch(r"\d{1,3}", p):
            out.append(int(p))
    return out


class ArchiveScraper:
    def __init__(self, fetcher):
        self.f = fetcher
        self._form = {}
        # the sid / nonce are tied to the HTTP session, so re-fetch them
        # whenever the fetcher rebuilds it
        self.f.on_reset = self._form.clear

    def _form_tokens(self, slug):
        """Fetch the page once to pick up the per-session `sid` / nonce tokens."""
        if slug in self._form:
            return self._form[slug]
        url = f"{BASE}/{slug}/"
        text, _ = self.f.get(url, use_cache=False)
        tok = {}
        m = re.search(r'name="sid" value="([^"]+)"', text)
        if m:
            tok["sid"] = m.group(1)
        m = re.search(r'name="form_nonce" value="([^"]+)"', text)
        if m:
            tok["form_nonce"] = m.group(1)
            tok["_wp_http_referer"] = f"/{slug}/"
        self._form[slug] = tok
        return tok

    def fetch_month(self, game, year, month_name):
        slug, fmonth, fyear, fsubmit, _ = PAGES[game]
        url = f"{BASE}/{slug}/"
        data = {fmonth: month_name, fyear: str(year), fsubmit: "SEARCH"}
        data.update(self._form_tokens(slug))
        text, _ = self.f.post(url, data)
        return text, url

    # ------------------------------------------------------------------
    def parse(self, game, html, url):
        """Return a list of draw dicts from one month page."""
        soup = BeautifulSoup(html, "html.parser")
        results = soup.find("div", id="results")
        if results is None:
            return []
        table = results.find("table")
        if table is None:
            return []

        headers = [_norm_header(th.get_text()) for th in table.find_all("th")]
        # the site emits a malformed nested <tr><th> in the first cell
        headers = [re.sub(r"^.*?draw$", "draw", h) if "draw" in h else h for h in headers]
        if not headers:
            return []

        rows = []
        cur_date = None
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            texts = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in cells]
            if len(cells) == 1 or (len(texts) == 1):
                d = nz.parse_archive_date(texts[0])
                if d:
                    cur_date = d
                continue
            row = dict(zip(headers, texts))
            rec = self._to_record(game, row, cur_date, texts, headers, url)
            if rec:
                rows.append(rec)
        return rows

    def _to_record(self, game, row, cur_date, texts, headers, url):
        draw_number = nz.parse_int(row.get("draw"))
        if draw_number is None:
            return None

        date = nz.parse_archive_date(row.get("date")) if row.get("date") else None
        date = date or cur_date

        period = nz.normalize_period(row.get("time")) if row.get("time") else None
        time = nz.PERIOD_TIMES.get((row.get("time") or "").strip().lower())
        if time is None:
            time = nz.parse_time(row.get("time"))

        numbers, bonus = self._numbers(game, row, headers)
        if not numbers:
            return None

        jackpot = nz.money_to_cents(row.get("jackpot"))
        wins = nz.parse_int(row.get("wins"))
        mult = row.get("multiplier")
        if mult and mult.upper() in ("NA", "N/A", "NO DATA", "X", ""):
            mult = None
        promo = row.get("promo") or None

        return {
            "game": game,
            "draw_number": draw_number,
            "draw_date": date,
            "draw_time": time,
            "draw_period": period,
            "dow": nz.dow(date),
            "numbers": numbers,
            "bonus_number": bonus,
            "multiplier": mult,
            "jackpot_cents": jackpot,
            "wins": wins,
            "promo": promo,
            "source": SOURCE,
            "source_url": url,
            "raw": row,
        }

    @staticmethod
    def _numbers(game, row, headers):
        """Extract (main numbers, bonus number) using whichever columns exist."""
        bonus = None
        for key in ("power ball", "powerball", "cash ball", "cashball",
                    "mega ball", "megaball", "bonus"):
            if key in row:
                bonus = nz.parse_int(row[key])
                break

        if "numbers" in row:
            nums = _split_numbers(row["numbers"])
        else:
            digit_cols = sorted(
                (h for h in headers if re.fullmatch(r"#\d+", h)),
                key=lambda h: int(h[1:]),
            )
            if digit_cols:
                nums = [nz.parse_int(row.get(h)) for h in digit_cols]
                nums = [n for n in nums if n is not None]
            elif "mark" in row:                       # Play Whe: a single mark 1-36
                n = nz.parse_int(row["mark"])
                nums = [n] if n is not None else []
            elif "number" in row:
                nums = _split_numbers(row["number"])
            else:
                nums = []

        if game == "pick4" and len(nums) == 1 and 0 <= nums[0] <= 9999:
            s = f"{nums[0]:04d}"
            nums = [int(c) for c in s]
        return nums, bonus

    # ------------------------------------------------------------------
    def iter_game(self, game, year_from=None, year_to=None):
        slug, _, _, _, first = PAGES[game]
        y0 = max(year_from or first, first)
        y1 = year_to or 2026
        for year in range(y0, y1 + 1):
            for month in MONTHS:
                try:
                    html, url = self.fetch_month(game, year, month)
                except Exception as e:                    # noqa: BLE001
                    log.error("fetch %s %s-%s failed: %s", game, month, year, e)
                    yield year, month, [], str(e)
                    continue
                try:
                    rows = self.parse(game, html, url)
                except Exception as e:                    # noqa: BLE001
                    log.error("parse %s %s-%s failed: %s", game, month, year, e)
                    yield year, month, [], str(e)
                    continue
                yield year, month, rows, None
