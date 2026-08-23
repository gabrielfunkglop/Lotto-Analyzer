#!/usr/bin/env python
"""Keep the archive backfill running unattended.

The mirror throttles hard and the crawl takes hours, so the scrape is designed
to be restartable: every finished month is recorded in `ingest_log` and every
fetched page is cached on disk. This supervisor just restarts the scraper if it
dies and stops once no months are outstanding.

    python supervise.py --max-hours 12
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

from lotto import db
from lotto.sources.archive import MONTHS, PAGES

ROOT = Path(__file__).resolve().parent


def outstanding(games):
    con = db.connect()
    done = {r[0] for r in con.execute(
        "SELECT target FROM ingest_log WHERE source='nlcbplaywhelotto' AND status='ok'")}
    today = dt.date.today()
    todo = 0
    for g in games:
        first = PAGES[g][4]
        for year in range(first, today.year + 1):
            for mi, month in enumerate(MONTHS, 1):
                if dt.date(year, mi, 1) > today:
                    continue
                if f"{g}:{year}-{month}" not in done:
                    todo += 1
    con.close()
    return todo


def finish(args):
    print("[supervise] backfill complete", flush=True)
    if args.then_analyze:
        print("[supervise] running analyze.py", flush=True)
        subprocess.run([sys.executable, "-u", "analyze.py"], cwd=str(ROOT), check=False)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", default="lotto,cashpot,winforlife,playwhe,pick4,pick2")
    ap.add_argument("--delay", default="18")
    ap.add_argument("--jitter", default="7")
    ap.add_argument("--block-sleep", default="600")
    ap.add_argument("--max-hours", type=float, default=14.0)
    ap.add_argument("--restart-wait", type=float, default=120.0)
    ap.add_argument("--then-analyze", action="store_true",
                    help="run analyze.py once the backfill finishes")
    args = ap.parse_args()

    games = [g.strip() for g in args.games.split(",")]
    deadline = time.time() + args.max_hours * 3600
    attempt = 0

    while time.time() < deadline:
        todo = outstanding(games)
        print(f"[supervise] {dt.datetime.now():%H:%M:%S} {todo} months outstanding", flush=True)
        if todo == 0:
            return finish(args)
        attempt += 1
        log = ROOT / "logs_archive.log"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== supervisor attempt {attempt} at {dt.datetime.now()} =====\n")
            proc = subprocess.Popen(
                [sys.executable, "-u", "scrape.py", "archive", "--games", args.games,
                 "--from", "2000", "--delay", args.delay, "--jitter", args.jitter,
                 "--block-sleep", args.block_sleep],
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
            )
            rc = proc.wait(timeout=max(60, deadline - time.time()))
        print(f"[supervise] scraper exited rc={rc}", flush=True)
        if rc == 0 and outstanding(games) == 0:
            return finish(args)
        time.sleep(args.restart_wait)

    print("[supervise] time budget exhausted", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
