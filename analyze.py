#!/usr/bin/env python
"""Run the anomaly battery over data/lotto.db and write reports/report.md.

    python analyze.py                 # all games
    python analyze.py --games lotto
    python analyze.py --sims 10000    # tighter Monte-Carlo nulls (slower)

Findings are also written to the `findings` table so runs can be compared over
time. Every p-value is carried through Benjamini-Hochberg; the report ranks on
the resulting q-value, not on raw p.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

import numpy as np

from lotto import db
from lotto.analysis import core, suite

log = logging.getLogger("analyze")

ALL_GAMES = ["lotto", "playwhe", "pick2", "pick4", "cashpot", "winforlife", "fastcash"]


def store_inferred_wins(con, game, draws):
    inferred = suite.infer_jackpot_wins(draws)
    rows = []
    for i in inferred:
        pub = i.pop("published_wins", None)
        verdict = i.pop("verdict", "win")
        agrees = None
        if pub is not None and verdict != "undetermined":
            agrees = 1 if (pub > 0) == (verdict == "win" and i["confidence"] >= 0.6) else 0
        rows.append((game, i["draw_number"], i["draw_date"], i["prev_jackpot"],
                     i["this_jackpot"], i["next_jackpot"], i["method"], i["confidence"],
                     agrees))

    def _write():
        con.executemany(
            "INSERT INTO inferred_wins(game,draw_number,draw_date,prev_jackpot,this_jackpot,"
            "next_jackpot,method,confidence,agrees_with_published) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(game,draw_number) DO UPDATE SET this_jackpot=excluded.this_jackpot,"
            "next_jackpot=excluded.next_jackpot,confidence=excluded.confidence,"
            "method=excluded.method,agrees_with_published=excluded.agrees_with_published",
            rows)
        con.commit()

    db.retry_locked(_write)
    return len(inferred)


def coverage(con):
    rows = con.execute(
        "SELECT game, COUNT(DISTINCT draw_number) n, MIN(draw_date) d0, MAX(draw_date) d1, "
        "SUM(CASE WHEN jackpot_cents IS NOT NULL THEN 1 ELSE 0 END) jp, "
        "SUM(CASE WHEN wins IS NOT NULL THEN 1 ELSE 0 END) wn "
        "FROM draws WHERE draw_number IS NOT NULL GROUP BY game ORDER BY game"
    ).fetchall()
    return [dict(r) for r in rows]


def fmt_p(p):
    if p is None or not np.isfinite(p):
        return "n/a"
    if p < 1e-4:
        return f"{p:.2e}"
    return f"{p:.4f}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", default="all")
    ap.add_argument("--sims", type=int, default=3000)
    ap.add_argument("--out", default="reports/report.md")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    games = ALL_GAMES if args.games == "all" else [g.strip() for g in args.games.split(",")]
    con = db.init()

    all_results = []
    cov = {c["game"]: c for c in coverage(con)}
    inferred_counts = {}

    for game in games:
        if game not in cov or cov[game]["n"] < 50:
            log.info("skip %s (no data)", game)
            continue
        log.info("analysing %s (%d draws)", game, cov[game]["n"])
        res, draws = suite.run_game(con, game, sims=args.sims)
        inferred_counts[game] = store_inferred_wins(con, game, draws)
        all_results += res

    all_results += suite.test_winner_consistency(con)
    all_results += suite.test_winner_locations(con)

    qs = core.benjamini_hochberg([r.p_value for r in all_results])
    for r, q in zip(all_results, qs):
        r.q_value = float(q)

    run_ts = dt.datetime.now().isoformat(timespec="seconds")
    finding_rows = [
        (run_ts, r.game, r.test, r.scope,
         None if not np.isfinite(r.statistic) else r.statistic,
         None if not np.isfinite(r.dof) else r.dof,
         None if not np.isfinite(r.p_value) else r.p_value,
         None if not np.isfinite(r.q_value) else r.q_value,
         r.n, None if not np.isfinite(r.effect) else r.effect, r.detail)
        for r in all_results
    ]

    def _write_findings():
        con.executemany(
            "INSERT INTO findings(run_ts,game,test,scope,statistic,dof,p_value,q_value,n,"
            "effect,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)", finding_rows)
        con.commit()

    db.retry_locked(_write_findings)

    write_report(con, args.out, run_ts, all_results, cov, inferred_counts, args.sims)
    print(f"\n{len(all_results)} tests run; report written to {args.out}")

    tested = [r for r in all_results if np.isfinite(r.p_value)]
    sig = [r for r in tested if r.q_value < 0.05]
    print(f"{len(tested)} with a p-value, {len(sig)} significant at FDR 5%")
    for r in sorted(sig, key=lambda r: r.q_value)[:10]:
        print(f"  {r.game:11s} {r.test:28s} {r.scope:24s} p={fmt_p(r.p_value)} q={fmt_p(r.q_value)}")


def write_report(con, path, run_ts, results, cov, inferred_counts, sims):
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    tested = [r for r in results if np.isfinite(r.p_value)]
    tested.sort(key=lambda r: (r.q_value if np.isfinite(r.q_value) else 9, r.p_value))
    descriptive = [r for r in results if not np.isfinite(r.p_value)]

    L = []
    A = L.append
    A(f"# NLCB draw archive - statistical anomaly report\n")
    A(f"Generated {run_ts}. Monte-Carlo nulls used {sims:,} simulations.\n")

    A("## Data coverage\n")
    A("| game | draws | first | last | with jackpot | with published wins | inferred wins |")
    A("|---|---:|---|---|---:|---:|---:|")
    for g, c in sorted(cov.items()):
        A(f"| {g} | {c['n']:,} | {c['d0']} | {c['d1']} | {c['jp']:,} | {c['wn']:,} | "
          f"{inferred_counts.get(g, 0):,} |")
    A("")

    A("## How to read this\n")
    A(f"{len(tested)} hypothesis tests were run. At a 5% threshold you would expect "
      f"about {0.05 * len(tested):.0f} to look 'significant' even if every draw were "
      "perfectly fair, so raw p-values are not enough. The **q** column is the "
      "Benjamini-Hochberg false-discovery-rate adjustment across all tests in this run; "
      "treat q < 0.05 as the bar, and even then check the effect size before believing it.\n")
    A("Ball-frequency tests for games that draw several numbers at once use a "
      "Monte-Carlo null that simulates real without-replacement draws, because the "
      "textbook chi-square null is wrong for those.\n")

    A("## Results ranked by evidence\n")
    A("| game | test | scope | n | statistic | p | q | detail |")
    A("|---|---|---|---:|---:|---:|---:|---|")
    for r in tested:
        stat = "" if not np.isfinite(r.statistic) else f"{r.statistic:.3f}"
        A(f"| {r.game} | {r.test} | {r.scope} | {r.n:,} | {stat} | {fmt_p(r.p_value)} | "
          f"{fmt_p(r.q_value)} | {r.detail} |")
    A("")

    sig = [r for r in tested if np.isfinite(r.q_value) and r.q_value < 0.05]
    A("## Findings that survive multiple-testing correction\n")
    if not sig:
        A("None. Every test is consistent with fair draws once the false-discovery "
          "rate is controlled.\n")
    else:
        for r in sig:
            A(f"- **{r.game} / {r.test} ({r.scope})** - p={fmt_p(r.p_value)}, "
              f"q={fmt_p(r.q_value)}, n={r.n:,}. {r.detail}")
        A("")

    if descriptive:
        A("## Descriptive checks (no p-value)\n")
        for r in descriptive:
            A(f"- **{r.game} / {r.test} ({r.scope})** - {r.detail}")
        A("")

    A("## Jackpot wins: published vs inferred\n")
    rows = con.execute(
        "SELECT game, COUNT(*) n, SUM(CASE WHEN confidence>=0.6 THEN 1 ELSE 0 END) strong, "
        "SUM(agrees_with_published) agree, COUNT(agrees_with_published) checked "
        "FROM inferred_wins GROUP BY game"
    ).fetchall()
    if rows:
        A("| game | jackpot drops seen | high-confidence | checkable against published | agreed |")
        A("|---|---:|---:|---:|---:|")
        for r in rows:
            A(f"| {r['game']} | {r['n']} | {r['strong']} | {r['checked'] or 0} | {r['agree'] or 0} |")
        A("")
    A("A rolling jackpot only grows, so a fall between consecutive draws implies the "
      "earlier draw was won. Where the archive also publishes an explicit winner count "
      "the two are compared above, which is what makes the inference trustworthy on the "
      "draws where no count is published.\n")

    A("## Caveats\n")
    A("- Draw data below is merged from the official site (exact jackpots, recent only) "
      "and an unofficial mirror (25 years of history). The mirror marks some draws as "
      "unverified and leaves jackpot/win cells as `X` where it has no data.\n")
    A("- Winner location data exists only for the few dozen announcements the official "
      "site currently shows; it is not a complete win record and cannot support a "
      "geographic significance claim.\n")
    A("- A significant result here means 'this does not look like the idealised fair "
      "model', which can equally be a data-quality artefact, a rule change, or a "
      "machine/ball-set change - not evidence of wrongdoing.\n")

    p.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
