"""Official source: nlcbgames.com.

The site is behind a Sucuri JS challenge (handled in ``lotto.http``) but exposes
a full WordPress REST API with one custom post type per game. That API is the
authoritative record for recent draws - exact jackpot figures, draw times, and
the ``lotto-jackpot`` forward estimates - though it only retains roughly the
last year.

The ``/winners/`` page is the only place that carries *where* a ticket was sold,
so it is scraped separately.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from .. import normalize as nz

log = logging.getLogger(__name__)

BASE = "https://www.nlcbgames.com"
API = BASE + "/wp-json/wp/v2"
SOURCE = "nlcbgames"

# game code -> REST post type
RESULT_TYPES = {
    "lotto": "lotto-result",
    "playwhe": "play-whe-result",
    "pick2": "pick-2-result",
    "pick4": "pick-4-result",
    "cashpot": "cashpot-result",
    "fastcash": "fast-cash-result",
    "winforlife": "win-for-life-result",
}

# NLCB stores money in ACF as thousandths of a dollar (a $2,180,000 jackpot is
# recorded as 2180000000). Verified against the archive mirror's dollar figures.
ACF_MONEY_DIVISOR = 10  # value / 10 == cents


def _money(v):
    if v in (None, "", False):
        return None
    try:
        return int(round(float(v) / ACF_MONEY_DIVISOR))
    except (TypeError, ValueError):
        return None


def _nums_from_group(group, keys=None):
    """ACF number groups are dicts like {number_1: 5, number_2: 19, ...}."""
    if not isinstance(group, dict):
        return []
    if keys is None:
        keys = sorted((k for k in group if re.fullmatch(r"number_\d+", k)),
                      key=lambda k: int(k.split("_")[1]))
    out = []
    for k in keys:
        v = group.get(k)
        n = nz.parse_int(v)
        if n is not None:
            out.append(n)
    return out


class NlcbGamesScraper:
    def __init__(self, fetcher):
        self.f = fetcher

    # ------------------------------------------------------------------
    # results via REST
    # ------------------------------------------------------------------
    def iter_results(self, game, per_page=100, max_pages=None, order="desc"):
        cpt = RESULT_TYPES[game]
        page = 1
        while True:
            if max_pages and page > max_pages:
                return
            url = (f"{API}/{cpt}?per_page={per_page}&page={page}"
                   f"&orderby=date&order={order}&_fields=id,slug,date,link,acf")
            try:
                data, _ = self.f.get_json(url)
            except Exception as e:                        # noqa: BLE001
                log.error("%s page %s failed: %s", cpt, page, e)
                return
            if isinstance(data, dict) and data.get("code"):
                # rest_post_invalid_page_number => past the end; anything else
                # is worth knowing about rather than silently truncating
                if data.get("code") != "rest_post_invalid_page_number":
                    log.warning("%s page %s returned %s: %s", cpt, page, data.get("code"),
                                str(data.get("message"))[:200])
                return
            if not data:
                return
            for item in data:
                for rec in self.to_records(game, item):
                    yield rec
            if len(data) < per_page:
                return
            page += 1

    def iter_results_windowed(self, game, start="2000-01-01", end=None, step_days=7,
                              per_page=100):
        """Walk a post type in date windows instead of by page offset.

        Fast Cash has well over a hundred thousand posts. Plain pagination hits
        an undocumented depth limit part-way through, so each window is kept
        small enough to page through cleanly and the crawl advances by date.
        """
        import datetime as _dt
        cpt = RESULT_TYPES[game]
        cur = _dt.date.fromisoformat(start)
        last = _dt.date.fromisoformat(end) if end else _dt.date.today() + _dt.timedelta(days=1)
        while cur <= last:
            nxt = cur + _dt.timedelta(days=step_days)
            page = 1
            while True:
                url = (f"{API}/{cpt}?per_page={per_page}&page={page}"
                       f"&after={cur.isoformat()}T00:00:00"
                       f"&before={nxt.isoformat()}T00:00:00"
                       f"&orderby=date&order=asc&_fields=id,slug,date,link,acf")
                try:
                    data, _ = self.f.get_json(url)
                except Exception as e:                    # noqa: BLE001
                    log.error("%s %s..%s page %s failed: %s", cpt, cur, nxt, page, e)
                    break
                if isinstance(data, dict) or not data:
                    break
                for item in data:
                    for rec in self.to_records(game, item):
                        yield rec
                if len(data) < per_page:
                    break
                page += 1
            cur = nxt

    # a game's secondary jackpot draw: (derived code, ACF group, picks)
    JACKPOT_DRAWS = {
        "playwhe": ("playwhe_jackpot", "play_whe_jackpot_winning_numbers", 5),
        "fastcash": ("fastcash_jackpot", "fast_cash_jackpot_winning_numbers", 4),
    }

    def to_records(self, game, item):
        """One API item can describe two draws: the headline number and, for
        Play Whe and Fast Cash, a separate multi-ball jackpot draw."""
        out = []
        main = self.to_record(game, item)
        if main:
            out.append(main)

        spec = self.JACKPOT_DRAWS.get(game)
        if spec:
            code, group_key, picks = spec
            acf = item.get("acf") or {}
            group = acf.get(group_key) if isinstance(acf, dict) else None
            nums = _nums_from_group(group)
            if len(nums) == picks:
                base = main or self.to_record(game, item)
                rec = dict(base) if base else None
                if rec:
                    rec.update(game=code, numbers=nums, bonus_number=None, multiplier=None)
                    out.append(rec)
        return out

    def to_record(self, game, item):
        acf = item.get("acf") or {}
        if not isinstance(acf, dict):
            acf = {}
        hint = (item.get("date") or "")[:10]
        d = nz.parse_nlcb_date(acf.get("draw_date"), hint=hint)
        t = nz.parse_time(acf.get("draw_time"))
        draw_number = nz.parse_int(re.sub(r"-\d+$", "", item.get("slug") or ""))

        numbers, bonus, mult, promo_flag = [], None, None, None

        if game == "lotto":
            g = acf.get("lotto_winning_numbers") or {}
            numbers = _nums_from_group(g)
            bonus = nz.parse_int(g.get("power_ball"))
            mult = g.get("multiplier_") or g.get("multiplier")
        elif game == "playwhe":
            g = acf.get("play_whe_winning_numbers") or {}
            n = nz.parse_int(g.get("winning_number"))
            numbers = [n] if n is not None else []
        elif game == "pick2":
            g = acf.get("pick_2_winning_numbers") or {}
            numbers = _nums_from_group(g, ["number_1", "number_2"])
            # `mega_ball` is a 0/1 flag for whether the Mega Ball add-on ran,
            # not a drawn ball, so it belongs in `promo`
            mb = nz.parse_int(g.get("mega_ball"))
            promo_flag = f"mega_ball={mb}" if mb is not None else None
        elif game == "pick4":
            v = acf.get("pick_4_winning_numbers")
            n = nz.parse_int(v)
            if n is not None:
                numbers = [int(c) for c in f"{n:04d}"]
        elif game == "cashpot":
            g = acf.get("cash_pot_winning_numbers") or {}
            numbers = _nums_from_group(g)
            mult = g.get("multiplier")
        elif game == "winforlife":
            g = acf.get("win_for_life_winning_numbers") or {}
            numbers = _nums_from_group(g)
            bonus = nz.parse_int(g.get("cash_ball"))
        elif game == "fastcash":
            g = acf.get("fast_cash_winning_numbers") or {}
            n = nz.parse_int(g.get("winning_number"))
            numbers = [n] if n is not None else []

        if not numbers:
            return None

        jackpot = _money(acf.get("jackpot"))
        return {
            "game": game,
            "draw_number": draw_number,
            "draw_date": d,
            "draw_time": t,
            "draw_period": nz.period_from_time(t),
            "dow": nz.dow(d),
            "numbers": numbers,
            "bonus_number": bonus,
            "multiplier": str(mult) if mult not in (None, "", False) else None,
            "jackpot_cents": jackpot,
            "wins": None,
            "promo": promo_flag,
            "source": SOURCE,
            "source_url": item.get("link"),
            "raw": {"slug": item.get("slug"), "post_date": item.get("date"), "acf": acf},
        }

    # ------------------------------------------------------------------
    # forward jackpot estimates
    # ------------------------------------------------------------------
    def iter_jackpots(self, per_page=100):
        page = 1
        while True:
            url = (f"{API}/lotto-jackpot?per_page={per_page}&page={page}"
                   f"&orderby=date&order=desc&_fields=slug,acf")
            data, _ = self.f.get_json(url)
            if isinstance(data, dict) or not data:
                return
            for item in data:
                acf = item.get("acf") or {}
                if not isinstance(acf, dict):
                    continue
                n = nz.parse_int(re.sub(r"-\d+$", "", item.get("slug") or ""))
                if n is None:
                    continue
                yield {
                    "game": "lotto",
                    "draw_number": n,
                    "jackpot_cents": _money(acf.get("current_jackpot")),
                    "kind": "current",
                    "source": SOURCE,
                }
            if len(data) < per_page:
                return
            page += 1

    # ------------------------------------------------------------------
    # winners page (location data)
    # ------------------------------------------------------------------
    # tab index -> game, in the order the four Oxygen tabs are emitted. The tab
    # images name them: Play Whe Jackpot, Fast Cash Jackpot, Cash Pot, Lotto Plus.
    WINNER_TABS = ["playwhe", "fastcash", "cashpot", "lotto"]

    def fetch_winners(self):
        html, _ = self.f.get(BASE + "/winners/", use_cache=False)
        return html

    def parse_winners(self, html):
        """Each tab uses its own card layout, so each gets its own parser."""
        soup = BeautifulSoup(html, "html.parser")
        wrap = soup.find("div", class_="oxy-tabs-contents-wrapper")
        out = []
        if wrap is None:
            return out
        parsers = {
            "playwhe": _card_playwhe,
            "fastcash": _card_fastcash,
            "cashpot": _card_cashpot,
            "lotto": _card_lotto,
        }
        tabs = wrap.find_all("div", class_="oxy-tab-content", recursive=False)
        for idx, tab in enumerate(tabs):
            game = self.WINNER_TABS[idx] if idx < len(self.WINNER_TABS) else f"tab{idx}"
            fn = parsers.get(game)
            if fn is None:
                continue
            lst = tab.find("div", class_="oxy-dynamic-list")
            for card in (lst.find_all("div", recursive=False) if lst else []):
                try:
                    rec = fn(card)
                except Exception:                        # noqa: BLE001
                    log.exception("winner card parse failed (%s)", game)
                    continue
                if rec and (rec.get("amount_cents") or rec.get("numbers")):
                    rec.update(game=game, source=SOURCE, source_url=BASE + "/winners/")
                    out.append(rec)
        return out


# ---------------------------------------------------------------------------
# winner card parsers
# ---------------------------------------------------------------------------
def _amount(card):
    el = card.find("span", class_=re.compile(r"winneramt"))
    if el:
        return nz.money_to_cents(el.get_text(strip=True))
    el = card.find("div", class_="ct-code-block")
    return nz.money_to_cents(el.get_text(strip=True)) if el else None


def _balls(card, cls):
    out = []
    for div in card.find_all("div", class_=cls):
        n = nz.parse_int(div.get_text(" ", strip=True))
        if n is not None:
            out.append(n)
    return out


def _players(card):
    t = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    m = re.search(r"(\d+)\s+(?:lucky\s+)?player", t, re.I)
    return int(m.group(1)) if m else None


def _text_blocks(card):
    return [b.get_text("\n", strip=True) for b in card.find_all("div", class_="ct-text-block")]


def _card_playwhe(card):
    t = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    m = re.search(r"at the (.+?) draw on (.+?) winning", t, re.I)
    time_raw, date_raw = (m.group(1).strip(), m.group(2).strip()) if m else (None, None)
    location = None
    for s in _text_blocks(card):
        if not s or s.lower().startswith(("winners", "play don")):
            continue
        if "player" in s.lower() or re.fullmatch(r"[\d\s]+", s):
            continue
        location = s
        break
    return {
        "draw_date_raw": date_raw,
        "draw_date": parse_winner_date(date_raw),
        "draw_time": nz.parse_time(time_raw) if time_raw else None,
        "players": _players(card),
        "amount_cents": _amount(card),
        "numbers": _balls(card, "winnergamenumberpw"),
        "location_raw": location,
    }


def _card_fastcash(card):
    blocks = _text_blocks(card)
    date_raw = location = None
    for s in blocks:
        if re.search(r"\b\d{1,2}(st|nd|rd|th)\s+[A-Z][a-z]+,?\s+\d{4}", s):
            date_raw = s
        elif s and not s.lower().startswith(("we rolling", "play don")) \
                and "player" not in s.lower() and not re.fullmatch(r"[\d\s]+", s):
            location = location or s
    return {
        "draw_date_raw": date_raw,
        "draw_date": parse_winner_date(date_raw),
        "draw_time": None,
        "players": _players(card),
        "amount_cents": _amount(card),
        "numbers": _balls(card, "winnergamenumberfc"),
        "location_raw": location,
    }


def _card_cashpot(card):
    blocks = _text_blocks(card)
    date_raw = location = None
    take_next = False
    for s in blocks:
        if take_next and s:
            location = s.strip().strip('"')
            take_next = False
            continue
        if s.strip().upper() == "LUCKY LOCATION":
            take_next = True
        m = re.search(r"sold on\s+(.+)$", s, re.I)
        if m:
            date_raw = m.group(1).strip()
    return {
        "draw_date_raw": date_raw,
        "draw_date": parse_winner_date(date_raw),
        "draw_time": None,
        "players": _players(card),
        "amount_cents": _amount(card),
        "numbers": [],
        "location_raw": location,
    }


def _card_lotto(card):
    t = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*Draw\s*#\s*(\d+)", t, re.I)
    date_raw, draw_number = (m.group(1), int(m.group(2))) if m else (None, None)

    location = None
    m = re.search(r"sold at\s+(.*?)(?:Play don|$)", t, re.I)
    if m:
        location = m.group(1).strip()

    mult = None
    mm = re.search(r"MULTIPLIER\s+(\d+)", t, re.I)
    if mm:
        mult = mm.group(1)
    bonus = None
    mb = re.search(r"POWERBALL\s+(\d+)", t, re.I)
    if mb:
        bonus = int(mb.group(1))

    return {
        "draw_number": draw_number,
        "draw_date_raw": date_raw,
        "draw_date": None,          # resolved against the draws table (DD/MM vs MM/DD)
        "draw_time": None,
        "players": _players(card),
        "amount_cents": _amount(card),
        "numbers": _balls(card, "winnergamenumberlo"),
        "bonus_number": bonus,
        "multiplier": mult,
        "location_raw": location,
    }


# ---------------------------------------------------------------------------
_ORDINAL_ONLY = re.compile(r"^([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)$")
_FULL_ORDINAL = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Z][a-z]+),?\s+(\d{4})")


def parse_winner_date(raw, today=None):
    """Winner cards use three date shapes, one of which omits the year."""
    if not raw:
        return None
    raw = raw.strip()
    today = today or date.today()

    m = _FULL_ORDINAL.search(raw)                       # "Friday 9th January, 2026"
    if m:
        mon = nz.MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
            except ValueError:
                return None

    m = _ORDINAL_ONLY.match(raw)                        # "December 1st" - no year
    if m:
        mon = nz.MONTHS.get(m.group(1)[:3].lower())
        if not mon:
            return None
        day = int(m.group(2))
        for yr in (today.year, today.year - 1, today.year - 2):
            try:
                d = date(yr, mon, day)
            except ValueError:
                continue
            if d <= today:
                return d.isoformat()
    return None


_STREET_WORDS = re.compile(
    r"\b(street|st\.?|road|rd\.?|avenue|ave\.?|highway|h'?way|drive|boulevard|blvd|"
    r"lane|trace|junction|corner|cnr|plaza|mall|branch|shops?|lp\b|e\.?m\.?r\.?|"
    r"s\.?m\.?r\.?|main rd|main road|circular|extension|village|court|terrace)\b",
    re.I)
_STARTS_ADDRESS = re.compile(r"^\s*(#|no\.?\s*\d|lp\s*#?\s*\d|\d)", re.I)


def _looks_like_address(line):
    return bool(_STARTS_ADDRESS.match(line)) or bool(_STREET_WORDS.search(line))


def split_locations(location_raw):
    """Split a winner card's location text into (outlet, address, area) triples.

    The four card layouts write locations differently. Play Whe and Fast Cash use
    one line, sometimes `Name, Area`, sometimes several outlets joined by `;`.
    Cash Pot lists several outlets as alternating name and address lines, so a
    naive split on newlines turns every address into a phantom outlet.
    """
    if not location_raw:
        return []
    text = location_raw.replace(";", "\n")
    lines = [ln.strip().strip('"').strip(",").strip()
             for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []

    entries = []
    for ln in lines:
        if entries and entries[-1][1] is None and _looks_like_address(ln) \
                and not _looks_like_address(entries[-1][0]):
            entries[-1][1] = ln
        else:
            entries.append([ln, None])

    out = []
    for name, addr in entries:
        # a single line like "Crystal's Bar, Toco" carries its own area
        if addr is None and "," in name:
            head, tail = name.rsplit(",", 1)
            if not _looks_like_address(tail):
                out.append((head.strip(), None, tail.strip()))
                continue
        area = addr.rsplit(",", 1)[-1].strip() if addr and "," in addr else None
        out.append((name.strip(), addr, area))
    return out
