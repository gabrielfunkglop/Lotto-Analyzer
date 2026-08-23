#!/usr/bin/env python
"""Daily refresh: pull what is new, then re-run the analysis.

Deliberately small. It fetches only the most recent pages from the official API
and only the current and previous month from the archive mirror, so it costs
around 40 requests instead of the 1,500 a full backfill needs.

    python refresh.py                 # normal daily run
    python refresh.py --no-analyze    # ingest only

Install as a daily scheduled task with `python refresh.py --install-task`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path

from lotto import db, geo
from lotto.http import Fetcher
from lotto.sources import nlcbgames as ng
from lotto.sources.archive import MONTHS, PAGES, ArchiveScraper

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("refresh")

# how many 100-item API pages to re-read per game; Fast Cash runs hundreds of
# draws a day so it needs a deeper window than the once-or-twice-daily games
RECENT_PAGES = {
    "lotto": 1, "playwhe": 2, "pick2": 2, "pick4": 2,
    "cashpot": 1, "winforlife": 1, "fastcash": 12,
}


def refresh_official(con, games, delay=1.0):
    f = Fetcher(delay=delay, jitter=0.5, retries=5, cache=False)
    scraper = ng.NlcbGamesScraper(f)
    total = 0
    for game in games:
        n = 0
        for rec in scraper.iter_results(game, max_pages=RECENT_PAGES.get(game, 2)):
            if db.upsert_draw(con, rec) is not None:
                n += 1
        con.commit()
        db.log_ingest(con, "nlcbgames", f"refresh:{game}", "ok", n)
        log.info("official %-11s %d rows", game, n)
        total += n

    jn = 0
    for j in scraper.iter_jackpots():
        con.execute(
            "INSERT INTO jackpots(game,draw_number,jackpot_cents,kind,source) VALUES (?,?,?,?,?) "
            "ON CONFLICT(game,draw_number,kind) DO UPDATE SET jackpot_cents=excluded.jackpot_cents",
            (j["game"], j["draw_number"], j["jackpot_cents"], j["kind"], j["source"]))
        jn += 1
    con.commit()
    log.info("official jackpot estimates %d", jn)
    return total


def refresh_winners(con, delay=1.0):
    import scrape
    f = Fetcher(delay=delay, jitter=0.5, retries=5, cache=False)
    scraper = ng.NlcbGamesScraper(f)
    recs = scraper.parse_winners(scraper.fetch_winners())
    n = scrape.store_winners(con, recs)
    scrape.link_winners(con)
    matched, total = geo.geocode_rows(con, overwrite=True)
    log.info("winners %d announcements, %d/%d outlets placed", n, matched, total)
    return n


def refresh_archive(con, games, months_back=2, delay=20.0):
    """Only the last couple of months - that is where jackpot and win counts land."""
    f = Fetcher(delay=delay, jitter=6.0, retries=5, cache=False, block_sleep=600)
    scraper = ArchiveScraper(f)
    today = dt.date.today()
    targets = []
    for back in range(months_back):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        targets.append((y, MONTHS[m - 1]))

    total = 0
    for game in games:
        if game not in PAGES:
            continue
        for year, month in targets:
            if year < PAGES[game][4]:
                continue
            try:
                html, url = scraper.fetch_month(game, year, month)
                rows = scraper.parse(game, html, url)
            except Exception as e:                        # noqa: BLE001
                log.error("archive %s %s-%s: %s", game, month, year, str(e)[:120])
                db.log_ingest(con, "nlcbplaywhelotto", f"refresh:{game}:{year}-{month}",
                              "error", 0, str(e)[:300])
                continue
            n = sum(1 for r in rows if db.upsert_draw(con, r) is not None)
            con.commit()
            db.log_ingest(con, "nlcbplaywhelotto", f"{game}:{year}-{month}", "ok", n)
            log.info("archive  %-11s %s-%s %d rows", game, month, year, n)
            total += n
    return total


def install_task(name="NLCB Lotto Analyzer daily refresh", at="09:00"):
    """Register a Windows Scheduled Task that runs this script every day."""
    cmd = [
        "schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", name, "/ST", at,
        "/TR", f'"{sys.executable}" "{ROOT / "refresh.py"}"',
    ]
    print(" ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", default="lotto,playwhe,pick2,pick4,cashpot,winforlife,fastcash")
    ap.add_argument("--no-analyze", action="store_true")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip the mirror (it is slow and rate-limited)")
    ap.add_argument("--sims", type=int, default=3000)
    ap.add_argument("--install-task", action="store_true")
    ap.add_argument("--task-time", default="09:00")
    args = ap.parse_args(argv)

    if args.install_task:
        return install_task(at=args.task_time)

    logfile = ROOT / "logs_refresh.log"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    log.info("=== refresh start ===")
    games = [g.strip() for g in args.games.split(",")]
    con = db.init()

    try:
        refresh_official(con, games)
    except Exception:                                     # noqa: BLE001
        log.exception("official refresh failed")
    try:
        refresh_winners(con)
    except Exception:                                     # noqa: BLE001
        log.exception("winners refresh failed")
    if not args.no_archive:
        try:
            refresh_archive(con, games)
        except Exception:                                 # noqa: BLE001
            log.exception("archive refresh failed")

    if not args.no_analyze:
        log.info("running analysis")
        r = subprocess.run([sys.executable, "-u", "analyze.py", "--sims", str(args.sims)],
                           cwd=str(ROOT))
        log.info("analysis exited rc=%s", r.returncode)
    log.info("=== refresh done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
