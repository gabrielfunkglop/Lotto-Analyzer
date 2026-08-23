"""The anomaly test battery.

Every test returns ``core.Result`` objects. The caller pools them, applies
Benjamini-Hochberg, and only then decides what counts as a finding.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
from collections import Counter, defaultdict

import numpy as np

from . import core
from .core import Result

log = logging.getLogger(__name__)

# nlcbgames is the authoritative source; the archive mirror fills in history.
SOURCE_PRIORITY = ["nlcbgames", "nlcbplaywhelotto"]


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------
def load_draws(con, game):
    """One row per draw number, preferring the authoritative source, but taking
    jackpot / wins from whichever source actually has them."""
    rows = con.execute(
        "SELECT * FROM draws WHERE game=? AND draw_number IS NOT NULL "
        "AND draw_date IS NOT NULL ORDER BY draw_number, source", (game,)
    ).fetchall()

    merged = {}
    for r in rows:
        d = dict(r)
        d["numbers"] = json.loads(d["numbers"] or "[]")
        # the mirror writes 0-0-0-0-0 where it never obtained a result; those are
        # missing draws, not draws of zeros
        if d["numbers"] and all(x == 0 for x in d["numbers"]):
            continue
        key = d["draw_number"]
        if key not in merged:
            merged[key] = d
            continue
        cur = merged[key]
        better = SOURCE_PRIORITY.index(d["source"]) < SOURCE_PRIORITY.index(cur["source"]) \
            if d["source"] in SOURCE_PRIORITY and cur["source"] in SOURCE_PRIORITY else False
        base, other = (d, cur) if better else (cur, d)
        for f in ("jackpot_cents", "wins", "multiplier", "draw_time", "draw_period", "promo"):
            if base.get(f) is None and other.get(f) is not None:
                base[f] = other[f]
        base["source"] = base["source"] + "+" + other["source"] \
            if base["source"] != other["source"] else base["source"]
        merged[key] = base

    out = sorted(merged.values(), key=lambda d: (d["draw_date"], d["draw_number"]))
    return out


def supported_range(values, min_count=3):
    """(min, max) over values that occur often enough to be real balls.

    Guards every pool calculation against one-off transcription errors on the
    source pages.
    """
    if not values:
        return None
    freq = Counter(values)
    common = [v for v, c in freq.items() if c >= min_count]
    if not common:
        common = list(freq)
    return min(common), max(common)


def game_meta(con, game, draws=None):
    """Game parameters, with the number pools derived from the data.

    The declared pools are only a starting point: Win For Life's cash ball turns
    out to run 1-3 rather than 1-5, and its main pool reaches 28. Testing against
    a declared range that is wider than the real one manufactures structural
    zeros and a spectacular but meaningless chi-square, so the observed range
    wins and the discrepancy is recorded.
    """
    r = con.execute("SELECT * FROM games WHERE code=?", (game,)).fetchone()
    meta = dict(r) if r else {}
    meta["pool_note"] = None
    if not draws:
        return meta

    # Take the pool from values the archive uses repeatedly. A lone typo - Pick 2
    # draw 5459 is published as "32 38" against a 1-36 pool - would otherwise widen
    # the pool by two impossible balls and produce a colossal false chi-square.
    mains = supported_range([x for d in draws for x in d["numbers"]])
    if mains:
        lo, hi = mains
        if (lo, hi) != (meta.get("pool_min"), meta.get("pool_max")):
            meta["pool_note"] = (f"main pool taken from the data as {lo}-{hi} "
                                 f"(declared {meta.get('pool_min')}-{meta.get('pool_max')})")
        meta["pool_min"], meta["pool_max"] = lo, hi

    bonus_vals = [d["bonus_number"] for d in draws if d.get("bonus_number") is not None]
    bonuses = supported_range(bonus_vals)
    if bonuses and meta.get("bonus_name"):
        lo, hi = bonuses
        if (lo, hi) != (meta.get("bonus_pool_min"), meta.get("bonus_pool_max")):
            note = (f"{meta['bonus_name']} pool taken from the data as {lo}-{hi} "
                    f"(declared {meta.get('bonus_pool_min')}-{meta.get('bonus_pool_max')})")
            meta["pool_note"] = f"{meta['pool_note']}; {note}" if meta["pool_note"] else note
        meta["bonus_pool_min"], meta["bonus_pool_max"] = lo, hi

    picks = Counter(len(d["numbers"]) for d in draws).most_common(1)
    if picks:
        meta["picks"] = picks[0][0]

    eras = detect_pool_eras(draws)
    meta["eras"] = eras
    if len(eras) > 1:
        note = ("ball pool changed over time: "
                + "; ".join(f"{e['label']} {e['pool_min']}-{e['pool_max']}" for e in eras))
        meta["pool_note"] = f"{meta['pool_note']}; {note}" if meta["pool_note"] else note
        # the current era is what the declared pool should match
        meta["pool_min"], meta["pool_max"] = eras[-1]["pool_min"], eras[-1]["pool_max"]
    return meta


# ---------------------------------------------------------------------------
# pool eras
# ---------------------------------------------------------------------------
def detect_pool_eras(draws, min_draws=200):
    """Split the history wherever the size of the ball pool changed.

    NLCB has quietly re-sized pools more than once: Cash Pot ran 1-25 between
    September 2007 and April 2010 and 1-20 either side of that, and Lotto Plus
    dropped from 36 balls to 35 after September 2012. Testing ball frequencies
    across such a boundary makes the retired balls look ice-cold and swamps any
    real signal, so each era is tested on its own.

    Boundaries are found by rolling window first, then snapped to the exact draw
    where the extra ball appears or stops appearing - a calendar-year split is
    not good enough, since a mid-year change leaves months of the old pool inside
    the new era and manufactures a "cold ball" result.
    """
    seq = [d for d in draws if d.get("numbers")]
    if len(seq) < min_draws * 2:
        return []

    # A single mistyped number on a source page must not invent an era, so only
    # values the archive actually uses repeatedly count towards the pool size.
    # (Pick 2 draw 5459 is published as "32 38" although its pool is 1-36.)
    freq = Counter(x for d in seq for x in d["numbers"])
    supported = {v for v, c in freq.items() if c >= 3}
    if not supported:
        return []
    floor = min(supported)
    maxima = [max([x for x in d["numbers"] if x in supported] or [floor]) for d in seq]
    n = len(seq)
    w = max(60, min(400, n // 12))
    roll = [max(maxima[max(0, i - w // 2): i + w // 2 + 1]) for i in range(n)]

    # segment on the rolling maximum
    bounds = [0]
    for i in range(1, n):
        if roll[i] != roll[i - 1]:
            bounds.append(i)
    bounds.append(n)

    segs = []
    for a, b in zip(bounds, bounds[1:]):
        if b > a:
            segs.append([a, b, roll[a]])
    # drop segments too small to be a real era, folding them into the previous one
    cleaned = []
    for s in segs:
        if cleaned and (s[1] - s[0]) < min_draws:
            cleaned[-1][1] = s[1]
        else:
            cleaned.append(s)
    if len(cleaned) <= 1:
        return []

    # snap each boundary to the exact draw where the pool actually changed
    for i in range(1, len(cleaned)):
        prev_max, cur_max = cleaned[i - 1][2], cleaned[i][2]
        lo = cleaned[i - 1][0]
        hi = cleaned[i][1]
        if cur_max > prev_max:
            idx = next((j for j in range(lo, hi) if maxima[j] > prev_max), None)
        else:
            idx = next((j + 1 for j in range(hi - 1, lo - 1, -1) if maxima[j] > cur_max), None)
        if idx is not None and cleaned[i - 1][0] < idx < cleaned[i][1]:
            cleaned[i - 1][1] = idx
            cleaned[i][0] = idx

    out = []
    for a, b, hi in cleaned:
        block = seq[a:b]
        if len(block) < 100:
            continue
        nums = [x for d in block for x in d["numbers"] if x in supported]
        if not nums:
            continue
        lo, hi2 = min(nums), max(nums)
        # an era that ends up with the same pool as the previous one was a
        # detection artefact, not a real change - fold it back in
        if out and (out[-1]["pool_min"], out[-1]["pool_max"]) == (lo, hi2):
            out[-1]["draws"] += block
            out[-1]["label"] = (out[-1]["label"].split("..")[0] + ".." +
                                (block[-1].get("draw_date") or "?"))
            continue
        d0 = block[0].get("draw_date") or "?"
        d1 = block[-1].get("draw_date") or "?"
        out.append({"label": f"{d0}..{d1}", "pool_min": lo, "pool_max": hi2,
                    "draws": block})
    return out if len(out) > 1 else []


# ---------------------------------------------------------------------------
# A. uniformity
# ---------------------------------------------------------------------------
def _frequency_result(game, draws, pool_min, pool_max, k, scope, sims):
    pool = pool_max - pool_min + 1
    counts = np.zeros(pool)
    n = 0
    for d in draws:
        nums = [x for x in d["numbers"] if pool_min <= x <= pool_max]
        if len(nums) != k:
            continue
        n += 1
        for x in nums:
            counts[x - pool_min] += 1
    if n < 100:
        return None

    if k == 1:
        chi2, dof, p = core.chi2_uniform(counts)
        method = "chi2"
    else:
        # simulate the real k-of-pool sampling scheme for a correct null
        chi2, p = core.chi2_mc_fast(counts, n, pool, k, sims=sims)
        dof = float("nan")
        method = f"monte-carlo({sims})"

    exp = counts.sum() / pool
    z = (counts - exp) / np.sqrt(exp)
    hot = int(np.argmax(counts)) + pool_min
    cold = int(np.argmin(counts)) + pool_min
    return Result(
        game=game, test="number_frequency", scope=scope,
        statistic=chi2, dof=dof, p_value=p, n=n,
        effect=float(np.abs(z).max()),
        detail=(f"{n} draws, pool {pool_min}-{pool_max}, null={method}; "
                f"hottest {hot} ({int(counts[hot - pool_min])}, z={z[hot - pool_min]:+.2f}), "
                f"coldest {cold} ({int(counts[cold - pool_min])}, z={z[cold - pool_min]:+.2f})"),
        extra={"counts": counts.tolist(), "pool_min": pool_min},
    )


def test_number_frequency(game, draws, meta, sims=3000):
    """Ball frequency, tested separately within each ball-pool era."""
    k = meta["picks"]
    eras = meta.get("eras") or []
    out = []
    if len(eras) <= 1:
        r = _frequency_result(game, draws, meta["pool_min"], meta["pool_max"], k,
                              "all draws", sims)
        return [r] if r else []

    for era in eras:
        r = _frequency_result(game, era["draws"], era["pool_min"], era["pool_max"], k,
                              f"{era['label']} (pool {era['pool_min']}-{era['pool_max']})", sims)
        if r:
            out.append(r)
    out.append(Result(
        game=game, test="pool_eras", scope="ball pool changes",
        n=len(draws),
        detail=("pool resized during the archive: "
                + "; ".join(f"{e['label']} = {e['pool_min']}-{e['pool_max']} "
                            f"({len(e['draws'])} draws)" for e in eras)
                + ". Frequencies are tested within each era, never across them."),
    ))
    return out


def test_positional_frequency(game, draws, meta):
    """For ordered games each drawn position should be uniform on its own."""
    if not meta.get("ordered"):
        return []
    pool_min, pool_max = meta["pool_min"], meta["pool_max"]
    pool = pool_max - pool_min + 1
    k = meta["picks"]
    out = []
    for pos in range(k):
        counts = np.zeros(pool)
        n = 0
        for d in draws:
            nums = d["numbers"]
            if len(nums) != k:
                continue
            v = nums[pos]
            if pool_min <= v <= pool_max:
                counts[v - pool_min] += 1
                n += 1
        if n < 100:
            continue
        chi2, dof, p = core.chi2_uniform(counts)
        exp = n / pool
        z = (counts - exp) / np.sqrt(exp)
        out.append(Result(
            game=game, test="positional_frequency", scope=f"position {pos + 1}",
            statistic=chi2, dof=dof, p_value=p, n=n, effect=float(np.abs(z).max()),
            detail=f"position {pos + 1} of {k}: {n} values over {pool} outcomes",
            extra={"counts": counts.tolist(), "pool_min": pool_min},
        ))
    return out


def test_bonus_frequency(game, draws, meta):
    if not meta.get("bonus_name"):
        return []
    lo, hi = meta.get("bonus_pool_min"), meta.get("bonus_pool_max")
    vals = [d["bonus_number"] for d in draws if d.get("bonus_number") is not None]
    if len(vals) < 100:
        return []
    if len(set(vals)) < 3:
        # a two-valued "bonus" is an add-on flag, not a drawn ball
        return [Result(game=game, test="bonus_field_is_a_flag", scope=meta["bonus_name"],
                       n=len(vals),
                       detail=(f"{meta['bonus_name']} only ever takes "
                               f"{sorted(set(vals))}; treated as a promotion flag, "
                               "not tested for uniformity"))]
    lo = lo if lo is not None else min(vals)
    hi = hi if hi is not None else max(vals)
    observed_hi = max(vals)
    hi = max(hi, observed_hi)
    counts = np.zeros(hi - lo + 1)
    for v in vals:
        if lo <= v <= hi:
            counts[v - lo] += 1
    chi2, dof, p = core.chi2_uniform(counts)
    exp = counts.sum() / counts.size
    z = (counts - exp) / np.sqrt(exp)
    return [Result(
        game=game, test="bonus_frequency", scope=meta["bonus_name"],
        statistic=chi2, dof=dof, p_value=p, n=int(counts.sum()),
        effect=float(np.abs(z).max()),
        detail=f"{meta['bonus_name']} range {lo}-{hi} over {int(counts.sum())} draws",
        extra={"counts": counts.tolist(), "pool_min": lo},
    )]


# ---------------------------------------------------------------------------
# B. independence / sequence structure
# ---------------------------------------------------------------------------
def test_lag_repeat(game, draws, meta, max_lag=3, sims=4000):
    """Does a number carry over to a later draw more often than chance?

    Overlaps at a given lag share draws with their neighbours, so summing
    per-pair variances understates the true variance and inflates z. The null
    here is instead built by shuffling the draw order, which preserves the
    marginal ball frequencies and only destroys the ordering.
    """
    pool = meta["pool_max"] - meta["pool_min"] + 1
    k = meta["picks"]
    sets = [frozenset(d["numbers"]) for d in draws if len(d["numbers"]) == k]
    n_sets = len(sets)
    if n_sets < 200:
        return []

    def total_overlap(order, lag):
        return sum(len(order[i] & order[i + lag]) for i in range(len(order) - lag))

    out = []
    for lag in range(1, max_lag + 1):
        n = n_sets - lag
        obs = total_overlap(sets, lag)
        null = np.empty(sims)
        idx = np.arange(n_sets)
        for i in range(sims):
            perm = [sets[j] for j in core.RNG.permutation(idx)]
            null[i] = total_overlap(perm, lag)
        centre = float(null.mean())
        p = float((np.sum(np.abs(null - centre) >= abs(obs - centre)) + 1) / (sims + 1))
        out.append(Result(
            game=game, test="lag_repeat", scope=f"lag {lag}",
            statistic=float(obs), p_value=p, n=n,
            effect=float((obs - centre) / n),
            detail=(f"mean carry-over {obs / n:.4f} numbers vs {centre / n:.4f} under a "
                    f"shuffled-order null ({sims} permutations, pool {pool}, k={k})"),
        ))
    return out


def test_autocorrelation(game, draws, meta, lags=(1, 2, 3, 4, 5, 7, 10), sims=2000):
    """Serial correlation of the drawn value (single-number games only)."""
    if meta["picks"] != 1:
        return []
    series = [d["numbers"][0] for d in draws if len(d["numbers"]) == 1]
    if len(series) < 500:
        return []
    out = []
    for lag in lags:
        r, p = core.autocorr_permutation(series, lag, sims=sims)
        if not np.isfinite(p):
            continue
        out.append(Result(
            game=game, test="autocorrelation", scope=f"lag {lag}",
            statistic=float(r), p_value=p, n=len(series), effect=float(r),
            detail=f"Pearson r={r:+.4f} at lag {lag}, permutation null ({sims} sims)",
        ))
    return out


def test_pair_cooccurrence(game, draws, meta, top=5):
    """Do particular pairs of balls come up together more than chance allows?"""
    k = meta["picks"]
    if k < 2 or meta.get("ordered"):
        return []
    pool_min, pool_max = meta["pool_min"], meta["pool_max"]
    pool = pool_max - pool_min + 1
    pairs = Counter()
    n = 0
    for d in draws:
        nums = sorted({x for x in d["numbers"] if pool_min <= x <= pool_max})
        if len(nums) != k:
            continue
        n += 1
        for a, b in itertools.combinations(nums, 2):
            pairs[(a, b)] += 1
    if n < 300:
        return []

    n_pairs = pool * (pool - 1) // 2
    exp = n * (k * (k - 1) / 2) / n_pairs
    counts = np.array([pairs.get(p, 0) for p in
                       itertools.combinations(range(pool_min, pool_max + 1), 2)], dtype=float)
    chi2 = float(((counts - exp) ** 2 / exp).sum())
    dof = n_pairs - 1
    from scipy import stats as _st
    p = float(_st.chi2.sf(chi2, dof))
    hottest = pairs.most_common(top)
    return [Result(
        game=game, test="pair_cooccurrence", scope="all pairs",
        statistic=chi2, dof=float(dof), p_value=p, n=n,
        effect=float((counts.max() - exp) / np.sqrt(exp)),
        detail=(f"{n_pairs} pairs, expected {exp:.1f} each; hottest "
                + ", ".join(f"{a}+{b}:{c}" for (a, b), c in hottest)),
    )]


def test_consecutive_numbers(game, draws, meta):
    """Fraction of draws containing at least one consecutive pair."""
    k = meta["picks"]
    if k < 2 or meta.get("ordered"):
        return []
    pool_min, pool_max = meta["pool_min"], meta["pool_max"]
    pool = pool_max - pool_min + 1
    hits = n = 0
    for d in draws:
        nums = sorted({x for x in d["numbers"] if pool_min <= x <= pool_max})
        if len(nums) != k:
            continue
        n += 1
        if any(b - a == 1 for a, b in zip(nums, nums[1:])):
            hits += 1
    if n < 300:
        return []
    from math import comb
    p_none = comb(pool - k + 1, k) / comb(pool, k)
    p_exp = 1 - p_none
    pv = core.binom_test(hits, n, p_exp)
    return [Result(
        game=game, test="consecutive_numbers", scope="any adjacent pair",
        statistic=float(hits), p_value=pv, n=n, effect=float(hits / n - p_exp),
        detail=f"{hits}/{n} draws ({hits / n:.3%}) contain consecutive numbers vs {p_exp:.3%} expected",
    )]


def _draws_are_sets(draws, k):
    """True when a draw is k distinct numbers with order irrelevant.

    Pick 4 draws ordered digits with repetition allowed, so the combinatorics
    below do not apply to it.
    """
    return all(len(set(d["numbers"])) == len(d["numbers"]) for d in draws
               if len(d["numbers"]) == k)


def test_repeat_sets(game, draws, meta):
    """Exact repeats of a full number set."""
    k = meta["picks"]
    if k < 3 or meta.get("ordered") or not _draws_are_sets(draws, k):
        return []
    pool = meta["pool_max"] - meta["pool_min"] + 1
    seen = Counter()
    n = 0
    for d in draws:
        nums = tuple(sorted(d["numbers"]))
        if len(nums) != k:
            continue
        seen[nums] += 1
        n += 1
    if n < 300:
        return []
    from math import comb
    total_sets = comb(pool, k)
    repeats = sum(c - 1 for c in seen.values() if c > 1)
    exp = n * (n - 1) / 2 / total_sets
    pv = core.poisson_exact_p(repeats, exp)
    dupes = [f"{'-'.join(map(str, s))} x{c}" for s, c in seen.most_common(3) if c > 1]
    return [Result(
        game=game, test="repeat_sets", scope="identical number sets",
        statistic=float(repeats), p_value=pv, n=n, effect=float(repeats - exp),
        detail=(f"{repeats} repeated sets over {n} draws vs {exp:.2f} expected "
                f"({total_sets:,} possible sets)" + ("; " + ", ".join(dupes) if dupes else "")),
    )]


def test_pick4_digits(game, draws, meta):
    if game != "pick4":
        return []
    out = []
    quads = [d["numbers"] for d in draws if len(d["numbers"]) == 4]
    if len(quads) < 300:
        return []
    n = len(quads)

    sums = Counter(sum(q) for q in quads)
    # exact distribution of the sum of four uniform digits
    from itertools import product
    exact = Counter(sum(c) for c in product(range(10), repeat=4))
    keys = sorted(exact)
    obs = np.array([sums.get(kk, 0) for kk in keys], dtype=float)
    expp = np.array([exact[kk] for kk in keys], dtype=float)
    expp = expp / expp.sum() * n
    keep = expp >= 5
    chi2 = float(((obs[keep] - expp[keep]) ** 2 / expp[keep]).sum())
    from scipy import stats as _st
    dof = int(keep.sum()) - 1
    out.append(Result(
        game=game, test="pick4_digit_sum", scope="sum of the four digits",
        statistic=chi2, dof=float(dof), p_value=float(_st.chi2.sf(chi2, dof)), n=n,
        detail=f"digit-sum distribution over {n} draws vs exact uniform-digit model",
    ))

    quads_t = [tuple(q) for q in quads]
    all_same = sum(1 for q in quads_t if len(set(q)) == 1)
    all_diff = sum(1 for q in quads_t if len(set(q)) == 4)
    out.append(Result(
        game=game, test="pick4_all_same", scope="quads (0000, 1111, ...)",
        statistic=float(all_same), p_value=core.binom_test(all_same, n, 10 / 10000),
        n=n, effect=float(all_same / n - 0.001),
        detail=f"{all_same} quads in {n} draws vs {n * 0.001:.1f} expected",
    ))
    out.append(Result(
        game=game, test="pick4_all_distinct", scope="four different digits",
        statistic=float(all_diff), p_value=core.binom_test(all_diff, n, 5040 / 10000),
        n=n, effect=float(all_diff / n - 0.504),
        detail=f"{all_diff}/{n} ({all_diff / n:.3%}) all-distinct vs 50.40% expected",
    ))
    return out


# ---------------------------------------------------------------------------
# C. conditioning on time
# ---------------------------------------------------------------------------
def _contingency_by(game, draws, meta, keyfn, test_name, label):
    pool_min, pool_max = meta["pool_min"], meta["pool_max"]
    pool = pool_max - pool_min + 1
    buckets = defaultdict(lambda: np.zeros(pool))
    n = 0
    for d in draws:
        key = keyfn(d)
        if key is None:
            continue
        for x in d["numbers"]:
            if pool_min <= x <= pool_max:
                buckets[key][x - pool_min] += 1
                n += 1
    if len(buckets) < 2 or n < 500:
        return []
    keys = sorted(buckets)
    table = np.array([buckets[k] for k in keys])
    chi2, dof, p, v = core.contingency(table)
    if not np.isfinite(p):
        return []
    return [Result(
        game=game, test=test_name, scope=label,
        statistic=chi2, dof=dof, p_value=p, n=n, effect=v,
        detail=(f"{len(keys)} groups ({', '.join(str(k) for k in keys[:8])}"
                f"{'...' if len(keys) > 8 else ''}) x {pool} numbers, Cramer V={v:.4f}"),
    )]


def test_by_period(game, draws, meta):
    return _contingency_by(game, draws, meta, lambda d: d.get("draw_period"),
                           "number_by_period", "draw period x number")


def test_by_dow(game, draws, meta):
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return _contingency_by(game, draws, meta,
                           lambda d: names[d["dow"]] if d.get("dow") is not None else None,
                           "number_by_dow", "day of week x number")


def test_by_era(game, draws, meta, block_years=3):
    def era(d):
        y = int(d["draw_date"][:4])
        return f"{y - (y % block_years)}s"
    return _contingency_by(game, draws, meta, era, "number_by_era",
                           f"{block_years}-year era x number")


def test_changepoints(game, draws, meta, top=3):
    """Flag balls whose appearance rate shifted mid-history."""
    pool_min, pool_max = meta["pool_min"], meta["pool_max"]
    pool = pool_max - pool_min + 1
    if len(draws) < 500:
        return []
    series = {v: [] for v in range(pool_min, pool_max + 1)}
    dates = []
    for d in draws:
        s = set(d["numbers"])
        dates.append(d["draw_date"])
        for v in series:
            series[v].append(1 if v in s else 0)
    scored = []
    for v, xs in series.items():
        i, mag = core.cusum_changepoint(xs)
        if i is not None and np.isfinite(mag):
            scored.append((mag, v, i))
    scored.sort(reverse=True)
    out = []
    for mag, v, i in scored[:top]:
        before = float(np.mean(series[v][:i + 1])) if i > 0 else float("nan")
        after = float(np.mean(series[v][i + 1:])) if i + 1 < len(dates) else float("nan")
        out.append(Result(
            game=game, test="rate_changepoint", scope=f"number {v}",
            statistic=float(mag), p_value=float("nan"), n=len(dates),
            effect=float(after - before) if np.isfinite(after) and np.isfinite(before) else float("nan"),
            detail=(f"CUSUM peak at {dates[i]} (draw index {i}): rate {before:.4f} -> {after:.4f}. "
                    "Descriptive only - CUSUM peaks exist in fair series too."),
        ))
    return out


# ---------------------------------------------------------------------------
# D. jackpots and winners
# ---------------------------------------------------------------------------
def jackpot_floor(draws):
    """The guaranteed minimum jackpot the game advertises.

    Lotto Plus sat at a flat $1,000,000 floor for years. It matters because the
    drop rule is blind at the floor: a draw already advertising the minimum is
    still advertising the minimum after it is won, so there is nothing to see.
    """
    vals = [d["jackpot_cents"] for d in draws if d.get("jackpot_cents")]
    if len(vals) < 20:
        return None
    arr = np.array(vals, dtype=float)
    lower = arr[arr <= np.percentile(arr, 40)]
    if lower.size == 0:
        return None
    mode = Counter(lower.tolist()).most_common(1)[0]
    # only trust it if that one value really does dominate the low tail
    return float(mode[0]) if mode[1] >= 0.15 * lower.size else float(arr.min())


def infer_jackpot_wins(draws):
    """Infer a jackpot win from a drop in the advertised jackpot.

    A rolling jackpot only ever grows; it falls back to the seed the draw after
    somebody wins, so a fall between draw i and draw i+1 implies draw i was won.

    Draws sitting at the advertised floor are marked `undetermined` rather than
    guessed at: both a win and a roll leave the figure unchanged there, and
    scoring them as negatives would flatter the rule.
    """
    seq = [d for d in draws if d.get("jackpot_cents")]
    floor = jackpot_floor(seq)
    inferred = []
    for i in range(len(seq) - 1):
        cur, nxt = seq[i], seq[i + 1]
        prev = seq[i - 1] if i else None
        at_floor = floor is not None and cur["jackpot_cents"] <= floor * 1.02
        ratio = nxt["jackpot_cents"] / cur["jackpot_cents"]

        if at_floor:
            verdict, conf, method = "undetermined", 0.0, "at_jackpot_floor"
        elif ratio < 0.7:
            verdict, conf, method = "win", 0.95, "jackpot_drop"
        elif ratio < 0.9:
            verdict, conf, method = "win", 0.7, "jackpot_drop"
        elif ratio < 1.0:
            verdict, conf, method = "win", 0.35, "jackpot_drop_small"
        else:
            continue                       # jackpot grew: rolled over, not a win

        inferred.append({
            "draw_number": cur["draw_number"],
            "draw_date": cur["draw_date"],
            "prev_jackpot": prev["jackpot_cents"] if prev else None,
            "this_jackpot": cur["jackpot_cents"],
            "next_jackpot": nxt["jackpot_cents"],
            "method": method,
            "verdict": verdict,
            "confidence": conf,
            "published_wins": cur.get("wins"),
        })
    return inferred


def undetermined_draws(draws):
    """Draw numbers where the jackpot floor makes the drop rule uninformative."""
    seq = [d for d in draws if d.get("jackpot_cents")]
    floor = jackpot_floor(seq)
    if floor is None:
        return set()
    return {d["draw_number"] for d in seq if d["jackpot_cents"] <= floor * 1.02}


def test_inference_accuracy(game, draws, meta):
    """Score the jackpot-drop rule against the published win counts."""
    if not meta.get("has_jackpot"):
        return []
    have = [d for d in draws if d.get("jackpot_cents") and d.get("wins") is not None]
    if len(have) < 50:
        return []
    won_rate = sum(1 for d in have if d["wins"] and d["wins"] > 0) / len(have)
    if won_rate > 0.5:
        return []      # pool game, not a rolling jackpot - see test_rollover_geometry
    blind = undetermined_draws(draws)
    scored = [d for d in have if d["draw_number"] not in blind]
    if len(scored) < 30:
        return []
    inferred = {i["draw_number"] for i in infer_jackpot_wins(draws)
                if i["verdict"] == "win" and i["confidence"] >= 0.6}
    tp = sum(1 for d in scored if d["wins"] > 0 and d["draw_number"] in inferred)
    fn = sum(1 for d in scored if d["wins"] > 0 and d["draw_number"] not in inferred)
    fp = sum(1 for d in scored if d["wins"] == 0 and d["draw_number"] in inferred)
    tn = sum(1 for d in scored if d["wins"] == 0 and d["draw_number"] not in inferred)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    return [Result(
        game=game, test="jackpot_drop_inference", scope="rule vs published wins",
        statistic=float(tp), p_value=float("nan"), n=len(scored),
        effect=float(prec) if np.isfinite(prec) else float("nan"),
        detail=(f"{len(have)} draws carry both a jackpot and a published win count; "
                f"{len(have) - len(scored)} sit at the advertised floor where the rule "
                f"cannot decide and are excluded. On the remaining {len(scored)}: "
                f"TP={tp} FP={fp} FN={fn} TN={tn}; precision={prec:.3f} recall={rec:.3f}"),
    )]


def test_rollover_geometry(game, draws, meta):
    """Rollover run lengths should look geometric if wins are memoryless."""
    if not meta.get("has_jackpot"):
        return []
    seq = [d for d in draws if d.get("wins") is not None]
    if len(seq) < 100:
        return []
    won = sum(1 for d in seq if d["wins"] and d["wins"] > 0)
    rate = won / len(seq)

    # Cash Pot's top prize is claimed on roughly 95% of draws: it is a daily pool,
    # not a rolling jackpot. Run-length geometry describes a jackpot that usually
    # rolls, so applying it here would manufacture an enormous, meaningless
    # chi-square. Report the rate instead.
    if rate > 0.5:
        return [Result(
            game=game, test="top_prize_frequency", scope="not a rolling jackpot",
            n=len(seq), effect=float(rate),
            detail=(f"the top prize is won on {won:,} of {len(seq):,} draws ({rate:.1%}), "
                    "so this game pays out a pool rather than rolling a jackpot. "
                    "Rollover-geometry and jackpot-drop inference do not apply to it."),
        )]

    runs, cur = [], 0
    for d in seq:
        cur += 1
        if d["wins"] and d["wins"] > 0:
            runs.append(cur)
            cur = 0
    phat, chi2, p, nruns = core.geometric_fit(runs)
    if nruns < 10:
        return []
    return [Result(
        game=game, test="rollover_geometry", scope="rollover run lengths",
        statistic=chi2, p_value=p, n=nruns, effect=float(phat),
        detail=(f"{nruns} jackpot wins over {len(seq)} draws; "
                f"per-draw win rate {phat:.4f} (mean roll {1 / phat:.1f} draws); "
                "chi-square tests how well a memoryless geometric model fits"),
    )]


def test_won_draw_number_profile(game, draws, meta):
    """Do won draws skew toward 'popular' low numbers (calendar-date picks)?"""
    if not meta.get("has_jackpot") or meta["pool_max"] < 31:
        return []
    won = [d for d in draws if d.get("wins") not in (None,) and d["wins"] > 0]
    lost = [d for d in draws if d.get("wins") == 0]
    if len(won) < 25 or len(lost) < 100:
        return []

    def low_share(ds):
        lo = sum(1 for d in ds for x in d["numbers"] if x <= 31)
        tot = sum(len(d["numbers"]) for d in ds)
        return lo, tot

    a, na = low_share(won)
    b, nb = low_share(lost)
    from scipy import stats as _st
    table = [[a, na - a], [b, nb - b]]
    chi2, p, dof, _ = _st.chi2_contingency(table)
    return [Result(
        game=game, test="won_draw_low_number_share", scope="numbers <= 31",
        statistic=float(chi2), dof=float(dof), p_value=float(p), n=na + nb,
        effect=float(a / na - b / nb),
        detail=(f"won draws {a / na:.3%} of balls <=31 vs {b / nb:.3%} on rolled draws "
                f"({len(won)} won, {len(lost)} rolled). A real effect here reflects "
                "player number choice, not draw bias."),
    )]


def test_out_of_pool(game, draws, meta):
    """Numbers a game cannot actually draw - i.e. transcription errors."""
    freq = Counter(x for d in draws for x in d["numbers"])
    if not freq:
        return []
    common = {v for v, c in freq.items() if c >= 3}
    if not common:
        return []
    lo, hi = min(common), max(common)
    bad = {v: c for v, c in freq.items() if v < lo or v > hi}
    if not bad:
        return []
    examples = []
    for d in draws:
        if any(x in bad for x in d["numbers"]):
            examples.append(f"draw {d['draw_number']} ({d['draw_date']}) {d['numbers']}")
        if len(examples) >= 4:
            break
    return [Result(
        game=game, test="out_of_pool_values", scope=f"outside {lo}-{hi}",
        n=sum(bad.values()), effect=float(len(bad)),
        detail=(f"{sum(bad.values())} ball value(s) outside the pool this game uses: "
                + ", ".join(f"{v} x{c}" for v, c in sorted(bad.items()))
                + ". Almost certainly typos on the source page. e.g. "
                + "; ".join(examples)),
    )]


def test_source_agreement(con, game):
    """Where both sources cover the same draw, do they publish the same numbers?

    This is a data-integrity check rather than a fairness test, and it is the
    only way to tell a genuine repeated draw from a transcription slip on one
    of the two sites.
    """
    rows = con.execute(
        "SELECT draw_number, source, numbers, bonus_number, draw_date FROM draws "
        "WHERE game=? AND draw_number IS NOT NULL ORDER BY draw_number", (game,)
    ).fetchall()
    by_draw = defaultdict(dict)
    for r in rows:
        by_draw[r["draw_number"]][r["source"]] = r

    both = {k: v for k, v in by_draw.items() if len(v) > 1}
    if len(both) < 20:
        return []

    mismatch_nums, mismatch_dates, examples = 0, 0, []
    for dn, srcs in sorted(both.items()):
        vals = list(srcs.values())
        sets = {frozenset(json.loads(v["numbers"] or "[]")) for v in vals}
        dates = {v["draw_date"] for v in vals}
        if len(sets) > 1:
            mismatch_nums += 1
            if len(examples) < 5:
                examples.append(
                    f"draw {dn}: " + " vs ".join(
                        f"{v['source']}={json.loads(v['numbers'] or '[]')}" for v in vals))
        if len(dates) > 1:
            mismatch_dates += 1

    rate = mismatch_nums / len(both)
    return [Result(
        game=game, test="source_agreement", scope="overlapping draws",
        statistic=float(mismatch_nums), p_value=float("nan"), n=len(both),
        effect=float(rate),
        detail=(f"{len(both)} draws carried by both sources; {mismatch_nums} disagree on the "
                f"numbers ({rate:.2%}) and {mismatch_dates} on the date."
                + (" e.g. " + "; ".join(examples) if examples else "")),
    )]


def test_duplicate_sets_by_source(con, game):
    """Repeated number sets, split by source, to localise any duplication."""
    out = []
    meta = con.execute("SELECT ordered FROM games WHERE code=?", (game,)).fetchone()
    if meta and meta["ordered"]:
        return []
    rows = con.execute(
        "SELECT source, numbers, draw_number, draw_date FROM draws WHERE game=? "
        "AND draw_number IS NOT NULL", (game,)
    ).fetchall()
    by_src = defaultdict(list)
    for r in rows:
        by_src[r["source"]].append(r)
    for src, rs in by_src.items():
        if len(rs) < 200:
            continue
        parsed = [json.loads(r["numbers"] or "[]") for r in rs]
        if any(len(set(p)) != len(p) for p in parsed):
            continue                      # repeats allowed within a draw: not a set
        counts = Counter(tuple(sorted(p)) for p in parsed)
        k = len(next(iter(counts)))
        if k < 3:
            continue
        pool = max(x for s in counts for x in s)
        from math import comb
        total_sets = comb(pool, k)
        n = len(rs)
        repeats = sum(c - 1 for c in counts.values() if c > 1)
        exp = n * (n - 1) / 2 / total_sets
        top = [f"{'-'.join(map(str, s))} x{c}" for s, c in counts.most_common(3) if c > 1]
        out.append(Result(
            game=game, test="repeat_sets_by_source", scope=src,
            statistic=float(repeats), p_value=core.poisson_exact_p(repeats, exp), n=n,
            effect=float(repeats - exp),
            detail=(f"{src}: {repeats} repeated sets in {n} draws vs {exp:.2f} expected"
                    + ("; " + ", ".join(top) if top else "")),
        ))
    return out


def test_money_calibration(con, game):
    """Check the assumed scale of the official site's ACF money fields.

    nlcbgames stores a $2,180,000 jackpot as the integer 2180000000, so the
    scraper divides by 10 to get cents. The archive mirror publishes the same
    jackpots as plain dollar strings, so any draw carried by both sources is a
    direct check on that assumption.
    """
    rows = con.execute(
        "SELECT a.draw_number, a.jackpot_cents AS official, b.jackpot_cents AS mirror "
        "FROM draws a JOIN draws b ON a.game=b.game AND a.draw_number=b.draw_number "
        "WHERE a.game=? AND a.source='nlcbgames' AND b.source='nlcbplaywhelotto' "
        "AND a.jackpot_cents IS NOT NULL AND b.jackpot_cents IS NOT NULL", (game,)
    ).fetchall()
    if len(rows) < 5:
        return []
    ratios = np.array([r["official"] / r["mirror"] for r in rows], dtype=float)
    med = float(np.median(ratios))
    agree = int(np.sum(np.abs(ratios - 1) < 0.02))
    return [Result(
        game=game, test="money_scale_calibration", scope="official vs mirror jackpots",
        statistic=med, p_value=float("nan"), n=len(rows),
        effect=float(agree / len(rows)),
        detail=(f"{len(rows)} draws carry a jackpot from both sources; median ratio "
                f"official/mirror = {med:.4f}, {agree}/{len(rows)} agree within 2%. "
                "A median far from 1.0 means the assumed ACF divisor is wrong."),
    )]


def test_winner_consistency(con):
    """Cross-check winner announcements against the draw archive."""
    rows = con.execute(
        "SELECT w.game, w.draw_number, w.draw_date, w.draw_date_raw, w.numbers, "
        "w.amount_cents, d.draw_date AS d_date, d.numbers AS d_numbers, "
        "d.jackpot_cents, d.wins "
        "FROM winners w JOIN draws d ON d.game=w.game AND d.draw_number=w.draw_number "
        "GROUP BY w.id"
    ).fetchall()
    if not rows:
        return []
    date_mismatch, num_mismatch, checked, examples = 0, 0, 0, []
    for r in rows:
        checked += 1
        raw = r["draw_date_raw"] or ""
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
        if m and r["d_date"]:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            iso = {f"{y:04d}-{mo:02d}-{da:02d}" for mo, da in ((b, a), (a, b))}
            if r["d_date"] not in iso:
                date_mismatch += 1
                if len(examples) < 5:
                    examples.append(f"draw {r['draw_number']}: card says {raw}, "
                                    f"archive says {r['d_date']}")
        wn = sorted(json.loads(r["numbers"] or "[]"))
        dn = sorted(json.loads(r["d_numbers"] or "[]"))
        if wn and dn and wn != dn:
            num_mismatch += 1
    if not checked:
        return []
    return [Result(
        game="all", test="winner_vs_draw_consistency", scope="linked announcements",
        statistic=float(date_mismatch), p_value=float("nan"), n=checked,
        effect=float(date_mismatch / checked),
        detail=(f"{checked} winner announcements linked to a draw; {date_mismatch} print a "
                f"date the draw archive disagrees with, {num_mismatch} print different numbers."
                + (" e.g. " + "; ".join(examples) if examples else "")),
    )]


def test_winner_locations(con, game=None):
    """Descriptive concentration of announced winners by outlet and area."""
    rows = con.execute(
        "SELECT w.game, wl.outlet, wl.area FROM winners w "
        "JOIN winner_locations wl ON wl.winner_id = w.id"
    ).fetchall()
    if not rows:
        return []
    out = []
    by_game = defaultdict(list)
    for r in rows:
        by_game[r["game"]].append((r["outlet"], r["area"]))
    for g, items in by_game.items():
        outlets = Counter(o for o, _ in items if o)
        areas = Counter(a for _, a in items if a)
        n = len(items)
        if n < 5:
            continue
        top_o = ", ".join(f"{k} x{v}" for k, v in outlets.most_common(3))
        top_a = ", ".join(f"{k} x{v}" for k, v in areas.most_common(3))
        out.append(Result(
            game=g, test="winner_location_concentration", scope="announced winners",
            statistic=float(max(outlets.values()) if outlets else 0),
            p_value=float("nan"), n=n,
            effect=float(len(outlets)),
            detail=(f"{n} named outlets across {len(outlets)} distinct outlets / "
                    f"{len(areas)} areas. Top outlets: {top_o or 'n/a'}. "
                    f"Top areas: {top_a or 'n/a'}. Sample far too small for a "
                    "significance test - descriptive only."),
        ))
    return out


# ---------------------------------------------------------------------------
def _era_slices(draws, meta):
    """Yield (draws, meta, tag) per ball-pool era, or one slice if the pool never moved."""
    eras = meta.get("eras") or []
    if len(eras) <= 1:
        yield draws, meta, None
        return
    for era in eras:
        sub = dict(meta)
        sub["pool_min"], sub["pool_max"] = era["pool_min"], era["pool_max"]
        sub["eras"] = []
        yield era["draws"], sub, f"pool {era['pool_min']}-{era['pool_max']}, {era['label']}"


def run_game(con, game, sims=3000):
    draws = load_draws(con, game)
    if len(draws) < 50:
        log.info("%s: only %d draws, skipping", game, len(draws))
        return [], draws
    meta = game_meta(con, game, draws)
    results = []
    if meta.get("pool_note"):
        log.info("%s: %s", game, meta["pool_note"])
        results.append(Result(game=game, test="pool_calibration", scope="declared vs observed",
                              n=len(draws), detail=meta["pool_note"]))
    for fn in (test_source_agreement, test_duplicate_sets_by_source, test_money_calibration):
        try:
            results += fn(con, game)
        except Exception as e:                            # noqa: BLE001
            log.exception("%s failed on %s: %s", fn.__name__, game, e)

    # tests whose null depends on the size of the ball pool have to be run
    # inside a single era, never across a pool change
    for fn in (test_pair_cooccurrence, test_consecutive_numbers, test_repeat_sets,
               test_lag_repeat, test_by_period, test_by_dow, test_by_era,
               test_changepoints):
        for sub_draws, sub_meta, tag in _era_slices(draws, meta):
            try:
                for r in fn(game, sub_draws, sub_meta):
                    if tag:
                        r.scope = f"{r.scope} [{tag}]"
                    results.append(r)
            except Exception as e:                        # noqa: BLE001
                log.exception("%s failed on %s: %s", fn.__name__, game, e)
    for fn in (test_number_frequency, test_positional_frequency, test_bonus_frequency,
               test_autocorrelation, test_pick4_digits, test_out_of_pool,
               test_inference_accuracy, test_rollover_geometry,
               test_won_draw_number_profile):
        try:
            if fn is test_number_frequency:
                results += fn(game, draws, meta, sims=sims)
            else:
                results += fn(game, draws, meta)
        except Exception as e:                            # noqa: BLE001
            log.exception("%s failed on %s: %s", fn.__name__, game, e)
    return results, draws
