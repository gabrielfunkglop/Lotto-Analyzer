#!/usr/bin/env python
"""Local web view over the NLCB draw archive.

    python serve.py                 # http://127.0.0.1:5000
    python serve.py --port 8080

Read-only: it never writes to the database, so it is safe to run while a scrape
is in progress.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from lotto import db
from lotto.analysis import suite

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))

GAME_ORDER = ["lotto", "playwhe", "playwhe_jackpot", "cashpot", "pick2", "pick4",
              "winforlife", "fastcash", "fastcash_jackpot"]


def get_db():
    con = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con


def games(con):
    rows = {r["code"]: dict(r) for r in con.execute("SELECT * FROM games")}
    cov = {r["game"]: dict(r) for r in con.execute(
        "SELECT game, COUNT(DISTINCT draw_number) n, MIN(draw_date) d0, MAX(draw_date) d1 "
        "FROM draws WHERE draw_number IS NOT NULL AND draw_date IS NOT NULL GROUP BY game")}
    out = []
    for code in GAME_ORDER:
        if code not in rows:
            continue
        g = rows[code]
        g.update(cov.get(code) or {"n": 0, "d0": None, "d1": None})
        if g["n"]:
            out.append(g)
    for code, g in rows.items():
        if code not in GAME_ORDER and cov.get(code):
            g.update(cov[code])
            out.append(g)
    return out


def latest_run(con):
    r = con.execute("SELECT MAX(run_ts) t FROM findings").fetchone()
    return r["t"] if r else None


def findings_for(con, run_ts, game=None):
    q = "SELECT * FROM findings WHERE run_ts=?"
    a = [run_ts]
    if game:
        q += " AND game=?"
        a.append(game)
    q += " ORDER BY (q_value IS NULL), q_value, p_value"
    return [dict(r) for r in con.execute(q, a)]


# ---------------------------------------------------------------------------
@app.route("/")
def index():
    con = get_db()
    run_ts = latest_run(con)
    fs = findings_for(con, run_ts) if run_ts else []
    tested = [f for f in fs if f["p_value"] is not None]
    sig = [f for f in tested if f["q_value"] is not None and f["q_value"] < 0.05]

    totals = con.execute(
        "SELECT COUNT(*) rows, COUNT(DISTINCT game) g FROM draws").fetchone()
    span = con.execute(
        "SELECT MIN(draw_date) d0, MAX(draw_date) d1 FROM draws "
        "WHERE draw_date IS NOT NULL").fetchone()
    wins = con.execute(
        "SELECT COUNT(*) n, SUM(amount_cents) amt FROM winners").fetchone()

    recent = [dict(r) for r in con.execute(
        "SELECT game, draw_number, draw_date, numbers, bonus_number, jackpot_cents "
        "FROM draws WHERE draw_date IS NOT NULL AND game NOT LIKE 'fastcash%' "
        "ORDER BY draw_date DESC, draw_number DESC LIMIT 12")]
    for r in recent:
        r["numbers"] = json.loads(r["numbers"] or "[]")

    return render_template(
        "index.html", games=games(con), run_ts=run_ts, sig=sig,
        n_tested=len(tested), totals=totals, span=span, wins=wins, recent=recent,
        nav="home")


_GAME_CACHE = {}


def game_view(con, code):
    """Draws + calibrated metadata for one game, memoised.

    Fast Cash alone is ~120k draws, and re-deriving its pool eras on every page
    view makes the site feel sluggish. The cache key includes the row count so a
    running scrape invalidates it on its own.
    """
    stamp = con.execute(
        "SELECT COUNT(*) c, MAX(id) m FROM draws WHERE game=?", (code,)).fetchone()
    key = (code, stamp["c"], stamp["m"])
    hit = _GAME_CACHE.get(code)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    draws = suite.load_draws(con, code)
    meta = suite.game_meta(con, code, draws) if draws else {}
    _GAME_CACHE[code] = (key, draws, meta)
    return draws, meta


W, H, PAD = 1000.0, 200.0, 6.0


def jackpot_plot(draws, max_points=900):
    """Pre-compute SVG geometry for the jackpot line.

    Doing this here rather than in the template keeps the win markers aligned
    with the line: they need the index into the full series, which a filtered
    Jinja loop cannot give.
    """
    series = [d for d in draws if d.get("jackpot_cents")]
    if len(series) < 2:
        return None
    step = max(1, len(series) // max_points)
    idx = list(range(0, len(series), step))
    if idx[-1] != len(series) - 1:
        idx.append(len(series) - 1)

    top = max(d["jackpot_cents"] for d in series)
    last = len(series) - 1

    def xy(i):
        d = series[i]
        x = i / last * W
        y = (H - PAD) - (d["jackpot_cents"] / top) * (H - 2 * PAD)
        return x, y

    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i) for i in idx))
    won_idx = [i for i in range(len(series)) if series[i].get("wins")]
    # a pool game is won nearly every draw; thousands of markers would be a solid
    # band and a very heavy page, so thin them and say so
    marker_step = max(1, len(won_idx) // 300)
    wins = [{"x": round(x, 1), "y": round(y, 1),
             "date": series[i]["draw_date"], "n": series[i]["wins"],
             "amount": series[i]["jackpot_cents"]}
            for i in won_idx[::marker_step] for x, y in [xy(i)]]
    return {
        "points": points, "wins": wins, "won": len(won_idx), "thinned": marker_step > 1,
        "top": top, "first": series[0]["draw_date"], "last": series[-1]["draw_date"],
        "n": len(series), "w": W, "h": H,
    }


def era_list(draws, meta):
    """Ball-pool eras, or one synthetic era covering everything if the pool held."""
    eras = meta.get("eras") or []
    if eras:
        return eras
    return [{
        "label": f"{draws[0]['draw_date']}..{draws[-1]['draw_date']}",
        "pool_min": meta["pool_min"], "pool_max": meta["pool_max"],
        "draws": draws,
    }]


def frequency_bars(era):
    """Per-ball counts and z-scores for the frequency chart."""
    lo, hi = era["pool_min"], era["pool_max"]
    counts = Counter()
    for d in era["draws"]:
        for x in d["numbers"]:
            if lo <= x <= hi:
                counts[x] += 1
    total = sum(counts.values())
    expected = total / (hi - lo + 1) if hi >= lo else 0
    bars = []
    for v in range(lo, hi + 1):
        c = counts.get(v, 0)
        bars.append({
            "n": v,
            "count": c,
            "z": round((c - expected) / expected ** 0.5, 2) if expected > 0 else 0,
            "pct": (c / expected * 100) if expected else 0,
        })
    return bars, total, expected


DRAWS_PER_PAGE = 50


def draw_page(con, code, page, query):
    """One page of the draw table, plus the total row count for the pager."""
    where, args = ["game=?", "draw_number IS NOT NULL"], [code]
    if query:
        where.append("(CAST(draw_number AS TEXT) LIKE ? OR draw_date LIKE ? OR numbers LIKE ?)")
        args += [f"%{query}%"] * 3
    wsql = " AND ".join(where)

    n_rows = con.execute(f"SELECT COUNT(*) c FROM draws WHERE {wsql}", args).fetchone()["c"]
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM draws WHERE {wsql} ORDER BY draw_date DESC, draw_number DESC "
        f"LIMIT ? OFFSET ?", (*args, DRAWS_PER_PAGE, (page - 1) * DRAWS_PER_PAGE))]
    for r in rows:
        r["numbers"] = json.loads(r["numbers"] or "[]")
    return rows, n_rows


@app.route("/game/<code>")
def game(code):
    con = get_db()
    row = con.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
    if row is None:
        abort(404)
    meta = dict(row)

    draws, full_meta = game_view(con, code)
    if not draws:
        abort(404)
    # the calibrated pool beats whatever the games table declares
    for field in ("pool_min", "pool_max", "picks"):
        meta[field] = full_meta.get(field, meta[field])

    eras = era_list(draws, full_meta)
    era_idx = request.args.get("era", type=int)
    if era_idx is None or not (0 <= era_idx < len(eras)):
        era_idx = len(eras) - 1
    era = eras[era_idx]

    bars, total, expected = frequency_bars(era)
    page = max(1, request.args.get("page", 1, type=int))
    query = (request.args.get("q") or "").strip()
    rows, n_rows = draw_page(con, code, page, query)
    run_ts = latest_run(con)

    return render_template(
        "game.html", meta=meta, games=games(con),
        bars=bars, era=era, eras=eras, era_idx=era_idx,
        total=total, expected=expected,
        per_year=sorted(Counter(d["draw_date"][:4] for d in draws if d.get("draw_date")).items()),
        rows=rows, page=page, per=DRAWS_PER_PAGE, n_rows=n_rows, q=query,
        findings=findings_for(con, run_ts, code) if run_ts else [],
        jackpot=jackpot_plot(draws) if meta["has_jackpot"] else None,
        n_draws=len(draws), nav="games")


@app.route("/findings")
def findings():
    con = get_db()
    run_ts = latest_run(con)
    fs = findings_for(con, run_ts) if run_ts else []
    runs = [r["t"] for r in con.execute(
        "SELECT DISTINCT run_ts t FROM findings ORDER BY t DESC LIMIT 20")]
    return render_template("findings.html", games=games(con), findings=fs,
                           run_ts=run_ts, runs=runs, nav="findings")


class Projection:
    """Equirectangular projection fitted to a bounding box.

    Trinidad and Tobago sit ~40 km apart with open sea between them, so one
    projection covering both wastes most of the canvas. Each island gets its own
    projection and Tobago is drawn as an inset, which is how maps of the country
    are normally laid out.
    """

    def __init__(self, lon0, lat0, lon1, lat1, x, y, w, h, pad=0.02):
        dlon, dlat = (lon1 - lon0), (lat1 - lat0)
        lon0 -= dlon * pad; lon1 += dlon * pad
        lat0 -= dlat * pad; lat1 += dlat * pad
        self.lon0, self.lat0, self.lon1, self.lat1 = lon0, lat0, lon1, lat1
        import math
        self.k = math.cos(math.radians((lat0 + lat1) / 2))
        span_x = (lon1 - lon0) * self.k
        span_y = (lat1 - lat0)
        scale = min(w / span_x, h / span_y)
        self.scale = scale
        # centre the island inside its box
        self.ox = x + (w - span_x * scale) / 2
        self.oy = y + (h - span_y * scale) / 2
        self.span_y = span_y

    def contains(self, lat, lon):
        return self.lon0 <= lon <= self.lon1 and self.lat0 <= lat <= self.lat1

    def xy(self, lat, lon):
        px = self.ox + (lon - self.lon0) * self.k * self.scale
        py = self.oy + (self.lat1 - lat) * self.scale
        return px, py

    def path(self, polygons):
        parts = []
        for poly in polygons:
            for ring in poly:
                pts = []
                for lon, lat in ring:
                    x, y = self.xy(lat, lon)
                    pts.append(f"{x:.2f},{y:.2f}")
                if pts:
                    parts.append("M" + "L".join(pts) + "Z")
        return " ".join(parts)


def build_map(geom, markers):
    """Return SVG-ready geometry: island paths, marker positions, inset frame."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]

    def bbox(poly_list):
        xs = [c[0] for p in poly_list for r in p for c in r]
        ys = [c[1] for p in poly_list for r in p for c in r]
        return min(xs), min(ys), max(xs), max(ys)

    # the larger island by bounding-box area is Trinidad
    ranked = sorted(polys, key=lambda p: -((bbox([p])[2] - bbox([p])[0]) *
                                           (bbox([p])[3] - bbox([p])[1])))
    trini, tobago = ranked[0], (ranked[1] if len(ranked) > 1 else None)

    VW, VH = 820.0, 520.0
    b = bbox([trini])
    main = Projection(b[0], b[1], b[2], b[3], 12, 28, 590, 478)
    layers = [{"path": main.path([trini]), "proj": main}]

    inset = None
    if tobago is not None:
        tb = bbox([tobago])
        inset = Projection(tb[0], tb[1], tb[2], tb[3], 630, 40, 176, 132)
        layers.append({"path": inset.path([tobago]), "proj": inset})

    pts = []
    for m in markers:
        proj = None
        if inset is not None and inset.contains(m["lat"], m["lon"]):
            proj = inset
        elif main.contains(m["lat"], m["lon"]):
            proj = main
        else:
            proj = inset if (inset and m["lat"] > 11.0) else main
        x, y = proj.xy(m["lat"], m["lon"])
        pts.append(dict(m, x=round(x, 1), y=round(y, 1)))

    biggest = max((m["count"] for m in markers), default=1)
    for p in pts:
        p["r"] = round(4.5 + 11 * (p["count"] / biggest) ** 0.6, 1)
    return {"w": VW, "h": VH, "layers": [l["path"] for l in layers], "points": pts,
            "inset_box": (626, 34, 184, 144) if inset else None}


@app.route("/winners")
def winners():
    con = get_db()
    rows = [dict(r) for r in con.execute(
        "SELECT w.id, w.game, w.draw_number, w.draw_date, w.draw_date_raw, w.draw_time, "
        "w.players, w.amount_cents, w.numbers, w.location_raw, "
        "wl.seq, wl.outlet, wl.address, wl.area, wl.place, wl.lat, wl.lon "
        "FROM winners w LEFT JOIN winner_locations wl ON wl.winner_id=w.id "
        "ORDER BY w.draw_date DESC NULLS LAST, w.id DESC, wl.seq")]

    by_winner = {}
    for r in rows:
        w = by_winner.setdefault(r["id"], {
            "id": r["id"], "game": r["game"], "draw_number": r["draw_number"],
            "draw_date": r["draw_date"], "draw_date_raw": r["draw_date_raw"],
            "draw_time": r["draw_time"], "players": r["players"],
            "amount_cents": r["amount_cents"],
            "numbers": json.loads(r["numbers"] or "[]"), "outlets": [],
        })
        if r["outlet"]:
            w["outlets"].append({"outlet": r["outlet"], "address": r["address"],
                                 "area": r["area"], "place": r["place"],
                                 "lat": r["lat"], "lon": r["lon"], "seq": r["seq"]})
    winners_list = list(by_winner.values())

    places = defaultdict(lambda: {"count": 0, "amount": 0, "games": Counter(), "outlets": set()})
    for w in winners_list:
        for o in w["outlets"]:
            if o["lat"] is None:
                continue
            p = places[o["place"]]
            p["count"] += 1
            p["amount"] += w["amount_cents"] or 0
            p["games"][w["game"]] += 1
            p["outlets"].add(o["outlet"])
            p["lat"], p["lon"] = o["lat"], o["lon"]
    # `place` stays the raw gazetteer key so it matches what is stored on each
    # winner card; `label` is the human-readable form
    markers = [{"place": k, "label": k.title(), "lat": v["lat"], "lon": v["lon"],
                "count": v["count"], "amount": v["amount"],
                "outlets": sorted(v["outlets"]), "games": dict(v["games"])}
               for k, v in places.items()]
    markers.sort(key=lambda m: -m["count"])

    unmapped = sum(1 for w in winners_list for o in w["outlets"] if o["lat"] is None)
    geom = json.loads((ROOT / "data" / "tt_geom.json").read_text(encoding="utf-8"))
    tmap = build_map(geom, markers)

    return render_template("winners.html", games=games(con), winners=winners_list,
                           markers=markers, tmap=tmap, unmapped=unmapped,
                           total_outlets=sum(len(w["outlets"]) for w in winners_list),
                           nav="winners")


@app.route("/data")
def data_quality():
    con = get_db()
    sources = [dict(r) for r in con.execute(
        "SELECT game, source, COUNT(*) n, MIN(draw_date) d0, MAX(draw_date) d1, "
        "SUM(jackpot_cents IS NOT NULL) jp, SUM(wins IS NOT NULL) wn "
        "FROM draws GROUP BY game, source ORDER BY game, source")]
    placeholders = [dict(r) for r in con.execute(
        "SELECT game, source, COUNT(*) n FROM draws "
        "WHERE numbers IN ('[0, 0, 0, 0, 0]','[0, 0]','[0]','[0, 0, 0, 0, 0, 0]') "
        "GROUP BY game, source")]
    ingest = [dict(r) for r in con.execute(
        "SELECT source, status, COUNT(*) n, MAX(ts) last FROM ingest_log "
        "GROUP BY source, status ORDER BY source, status")]
    errors = [dict(r) for r in con.execute(
        "SELECT source, target, note, ts FROM ingest_log WHERE status='error' "
        "ORDER BY ts DESC LIMIT 25")]
    conflicts = [dict(r) for r in con.execute(
        "SELECT a.game, a.draw_number, a.draw_date, a.numbers AS official, "
        "b.numbers AS mirror FROM draws a "
        "JOIN draws b ON a.game=b.game AND a.draw_number=b.draw_number "
        "WHERE a.source='nlcbgames' AND b.source='nlcbplaywhelotto' "
        "AND a.numbers <> b.numbers "
        "ORDER BY a.game, a.draw_number LIMIT 300")]
    for c in conflicts:
        c["official"] = json.loads(c["official"] or "[]")
        c["mirror"] = json.loads(c["mirror"] or "[]")
    overlap = {r["game"]: r["n"] for r in con.execute(
        "SELECT a.game, COUNT(*) n FROM draws a "
        "JOIN draws b ON a.game=b.game AND a.draw_number=b.draw_number "
        "WHERE a.source='nlcbgames' AND b.source='nlcbplaywhelotto' GROUP BY a.game")}

    run_ts = latest_run(con)
    quality = [f for f in (findings_for(con, run_ts) if run_ts else [])
               if f["test"] in ("source_agreement", "repeat_sets_by_source", "pool_eras",
                                "pool_calibration", "money_scale_calibration",
                                "winner_vs_draw_consistency", "bonus_field_is_a_flag")]
    return render_template("data.html", games=games(con), sources=sources,
                           placeholders=placeholders, ingest=ingest, errors=errors,
                           quality=quality, conflicts=conflicts, overlap=overlap,
                           nav="data")


# ---------------------------------------------------------------------------
@app.route("/api/draws/<code>")
def api_draws(code):
    con = get_db()
    limit = min(request.args.get("limit", 200, type=int), 5000)
    rows = [dict(r) for r in con.execute(
        "SELECT draw_number, draw_date, draw_time, numbers, bonus_number, multiplier, "
        "jackpot_cents, wins, source FROM draws WHERE game=? "
        "ORDER BY draw_date DESC, draw_number DESC LIMIT ?", (code, limit))]
    for r in rows:
        r["numbers"] = json.loads(r["numbers"] or "[]")
    return jsonify(rows)


@app.route("/api/findings")
def api_findings():
    con = get_db()
    run_ts = latest_run(con)
    return jsonify(findings_for(con, run_ts) if run_ts else [])


@app.template_filter("money")
def money(cents):
    if cents is None:
        return "-"
    return "${:,.2f}".format(cents / 100)


@app.template_filter("compactmoney")
def compactmoney(cents):
    if cents is None:
        return "-"
    v = cents / 100
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:,.0f}"


@app.template_filter("pval")
def pval(p):
    if p is None:
        return "-"
    if p < 1e-4:
        return f"{p:.1e}"
    return f"{p:.4f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    print(f"\n  NLCB Lotto Analyzer  ->  http://{a.host}:{a.port}\n")
    app.run(host=a.host, port=a.port, debug=a.debug)


if __name__ == "__main__":
    main()
