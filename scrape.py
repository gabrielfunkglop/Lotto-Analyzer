#!/usr/bin/env python
"""Ingest NLCB draw results into data/lotto.db.

    python scrape.py init
    python scrape.py archive    --games all --from 2000        # deep history
    python scrape.py nlcbgames  --games all                    # recent, authoritative
    python scrape.py winners                                   # who/where won
    python scrape.py all

The two sources are complementary and are both kept: the unofficial archive has
25 years of history plus explicit jackpot-winner counts for older draws, while
nlcbgames.com has exact jackpot figures and draw times for the last ~year.
Rows are keyed (game, draw_number, source) so the two never overwrite each other.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import sys

from lotto import db, geo
from lotto.http import Fetcher
from lotto.sources import nlcbgames as ng
from lotto.sources.archive import MONTHS, PAGES, ArchiveScraper

log = logging.getLogger("scrape")

ALL_GAMES = ["lotto", "playwhe", "pick2", "pick4", "cashpot", "winforlife"]


def _games_arg(value):
    if not value or value == "all":
        return list(ALL_GAMES)
    return [g.strip() for g in value.split(",") if g.strip()]


def _done_months(con, source):
    rows = con.execute(
        "SELECT target FROM ingest_log WHERE source=? AND status='ok'", (source,)
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
def cmd_archive(args):
    con = db.init()
    f = Fetcher(delay=args.delay, jitter=args.jitter, retries=8,
                burst=args.burst or None, cooldown=args.cooldown,
                block_sleep=args.block_sleep)
    scraper = ArchiveScraper(f)
    done = set() if args.refresh else _done_months(con, "nlcbplaywhelotto")

    today = dt.date.today()
    # never trust a cached copy of the current or previous month
    hot = {f"{today.year}-{MONTHS[today.month - 1]}"}
    prev = today.replace(day=1) - dt.timedelta(days=1)
    hot.add(f"{prev.year}-{MONTHS[prev.month - 1]}")

    total = 0
    for game in args.games:
        first = PAGES[game][4]
        y0 = max(args.year_from or first, first)
        y1 = args.year_to or today.year
        for year in range(y0, y1 + 1):
            for month in MONTHS:
                key = f"{game}:{year}-{month}"
                if key in done and f"{year}-{month}" not in hot:
                    continue
                if dt.date(year, MONTHS.index(month) + 1, 1) > today:
                    continue
                is_hot = f"{year}-{month}" in hot
                try:
                    if is_hot:
                        html, url = _fetch_fresh(scraper, game, year, month)
                    else:
                        html, url = scraper.fetch_month(game, year, month)
                    rows = scraper.parse(game, html, url)
                except Exception as e:                     # noqa: BLE001
                    log.error("%s %s-%s: %s", game, month, year, e)
                    db.log_ingest(con, "nlcbplaywhelotto", key, "error", 0, str(e)[:400])
                    continue

                n = 0
                for rec in rows:
                    if db.upsert_draw(con, rec) is not None:
                        n += 1
                con.commit()
                db.log_ingest(con, "nlcbplaywhelotto", key, "ok", n)
                total += n
                log.info("%-11s %s-%s  %3d rows (total %d)", game, month, year, n, total)
    print(f"archive: {total} draw rows written")


def _fetch_fresh(scraper, game, year, month):
    """Bypass the page cache for months that are still changing."""
    old = scraper.f.cache
    scraper.f.cache = False
    try:
        return scraper.fetch_month(game, year, month)
    finally:
        scraper.f.cache = old


# ---------------------------------------------------------------------------
def cmd_nlcbgames(args):
    con = db.init()
    f = Fetcher(delay=args.delay, jitter=args.jitter, retries=5, cache=False)
    scraper = ng.NlcbGamesScraper(f)

    games = list(args.games)
    if args.include_fastcash and "fastcash" not in games:
        games.append("fastcash")

    total = 0
    for game in games:
        n = 0
        max_pages = args.max_pages
        if game == "fastcash" and max_pages is None:
            max_pages = args.fastcash_pages
        # Fast Cash is far too large for offset pagination to reach the end
        stream = (scraper.iter_results_windowed(game, start=args.since, step_days=args.window_days)
                  if args.windowed or (game == "fastcash" and max_pages is None)
                  else scraper.iter_results(game, max_pages=max_pages))
        for rec in stream:
            if db.upsert_draw(con, rec) is not None:
                n += 1
            if n % 500 == 0 and n:
                con.commit()
                log.info("%s ... %d", game, n)
        con.commit()
        db.log_ingest(con, "nlcbgames", game, "ok", n)
        total += n
        log.info("%-11s %d rows", game, n)

    jn = 0
    for j in scraper.iter_jackpots():
        con.execute(
            "INSERT INTO jackpots(game,draw_number,jackpot_cents,kind,source) VALUES (?,?,?,?,?) "
            "ON CONFLICT(game,draw_number,kind) DO UPDATE SET jackpot_cents=excluded.jackpot_cents",
            (j["game"], j["draw_number"], j["jackpot_cents"], j["kind"], j["source"]),
        )
        jn += 1
    con.commit()
    db.log_ingest(con, "nlcbgames", "lotto-jackpot", "ok", jn)
    print(f"nlcbgames: {total} draw rows, {jn} jackpot estimates")


# ---------------------------------------------------------------------------
def cmd_winners(args):
    con = db.init()
    f = Fetcher(delay=args.delay, jitter=args.jitter, retries=5, cache=False)
    scraper = ng.NlcbGamesScraper(f)
    html = scraper.fetch_winners()
    recs = scraper.parse_winners(html)
    n = store_winners(con, recs)
    db.log_ingest(con, "nlcbgames", "winners", "ok", n)
    print(f"winners: {n} announcements")
    link_winners(con)
    # the locations were just rewritten, so the map coordinates go with them
    matched, total = geo.geocode_rows(con, overwrite=True)
    print(f"winners: {matched}/{total} outlets placed on the map")


def winner_fingerprint(r):
    """Stable identity for one announcement, with no NULLs in it."""
    parts = [r.get("game"), r.get("draw_number"), r.get("draw_date_raw"),
             r.get("draw_time"), r.get("amount_cents"), r.get("location_raw"),
             json.dumps(r.get("numbers") or [])]
    return hashlib.sha1("|".join("" if p is None else str(p)
                                 for p in parts).encode("utf-8")).hexdigest()


def store_winners(con, recs):
    n = 0
    for r in recs:
        cur = con.execute(
            "INSERT INTO winners(game,draw_number,draw_date,draw_date_raw,draw_time,players,"
            "amount_cents,numbers,location_raw,source,source_url,fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "numbers=excluded.numbers, draw_date=excluded.draw_date, players=excluded.players, "
            "draw_number=COALESCE(excluded.draw_number, draw_number) "
            "RETURNING id",
            (r["game"], r.get("draw_number"), r["draw_date"], r["draw_date_raw"], r["draw_time"],
             r["players"], r["amount_cents"], json.dumps(r["numbers"]), r["location_raw"],
             r["source"], r["source_url"], winner_fingerprint(r)),
        ).fetchone()
        if not cur:
            continue
        wid = cur[0]
        con.execute("DELETE FROM winner_locations WHERE winner_id=?", (wid,))
        for i, (outlet, address, area) in enumerate(ng.split_locations(r["location_raw"]), 1):
            con.execute("INSERT INTO winner_locations(winner_id,seq,outlet,address,area) "
                        "VALUES (?,?,?,?,?)", (wid, i, outlet, address, area))
        n += 1
    con.commit()
    return n


def link_winners(con):
    """Attach a draw_number to each winner announcement.

    The Lotto Plus cards print the draw number outright; the others print only
    the numbers and a day-and-month, so those are matched against the draw
    archive. The Lotto cards' `02/05/2026` is ambiguous, so the date is taken
    from the matched draw rather than guessed.
    """
    linked = 0
    for w in con.execute(
            "SELECT * FROM winners WHERE draw_number IS NOT NULL AND draw_date IS NULL"
    ).fetchall():
        d = con.execute("SELECT draw_date FROM draws WHERE game=? AND draw_number=? "
                        "AND draw_date IS NOT NULL LIMIT 1",
                        (w["game"], w["draw_number"])).fetchone()
        if d:
            con.execute("UPDATE winners SET draw_date=?, match_method='draw_number' WHERE id=?",
                        (d["draw_date"], w["id"]))
            linked += 1
    con.commit()

    for w in con.execute("SELECT * FROM winners WHERE draw_number IS NULL").fetchall():
        nums = sorted(json.loads(w["numbers"] or "[]"))
        if not nums:
            continue
        cands = con.execute(
            "SELECT draw_number, numbers, draw_date, draw_time FROM draws "
            "WHERE game=? AND draw_number IS NOT NULL", (w["game"],)
        ).fetchall()
        best, method = None, None
        for c in cands:
            if sorted(json.loads(c["numbers"] or "[]")) != nums:
                continue
            if w["draw_date"] and c["draw_date"] == w["draw_date"]:
                best, method = c["draw_number"], "numbers+date"
                break
            if best is None:
                best, method = c["draw_number"], "numbers"
        if best is not None:
            con.execute("UPDATE winners SET draw_number=?, match_method=? WHERE id=?",
                        (best, method, w["id"]))
            linked += 1
    con.commit()
    print(f"winners: {linked} linked to a draw")


# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, delay=5.0):
        sp.add_argument("--games", type=_games_arg, default="all")
        sp.add_argument("--delay", type=float, default=delay)
        sp.add_argument("--jitter", type=float, default=2.0)

    sp = sub.add_parser("init"); sp.set_defaults(func=lambda a: (db.init(), print("db ready")))

    sp = sub.add_parser("archive", help="deep history from nlcbplaywhelotto.com")
    common(sp, 5.0)
    sp.add_argument("--from", dest="year_from", type=int, default=None)
    sp.add_argument("--to", dest="year_to", type=int, default=None)
    sp.add_argument("--refresh", action="store_true", help="re-fetch months already ingested")
    sp.add_argument("--burst", type=int, default=0,
                    help="requests before a cooldown (0 = rely on --delay alone)")
    sp.add_argument("--cooldown", type=float, default=70.0)
    sp.add_argument("--block-sleep", type=float, default=600.0,
                    help="seconds to wait out an IP block; the mirror escalates "
                         "if you keep knocking")
    sp.set_defaults(func=cmd_archive)

    sp = sub.add_parser("nlcbgames", help="recent + authoritative from nlcbgames.com REST")
    common(sp, 1.0)
    sp.add_argument("--max-pages", type=int, default=None)
    sp.add_argument("--include-fastcash", action="store_true")
    sp.add_argument("--fastcash-pages", type=int, default=None,
                    help="cap pages for the very large fast-cash type")
    sp.add_argument("--windowed", action="store_true",
                    help="walk by date window rather than page offset")
    sp.add_argument("--since", default="2024-01-01",
                    help="start date for windowed walks")
    sp.add_argument("--window-days", type=int, default=7)
    sp.set_defaults(func=cmd_nlcbgames)

    sp = sub.add_parser("winners", help="winner announcements incl. outlet / area")
    common(sp, 1.0)
    sp.set_defaults(func=cmd_winners)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if isinstance(getattr(args, "games", None), str):
        args.games = _games_arg(args.games)
    args.func(args)


if __name__ == "__main__":
    main()
