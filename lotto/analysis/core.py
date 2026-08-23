"""Statistical primitives.

Two things matter more than the individual tests here:

1. Draws of k numbers *without replacement* do not give independent multinomial
   counts, so a textbook chi-square on ball frequencies has the wrong null. Where
   that bites, we use a Monte-Carlo null built by simulating fair draws.
2. Running ~200 tests over 25 years of data guarantees small p-values by chance.
   Every reported p is therefore carried through Benjamini-Hochberg and judged on
   its q-value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import stats

RNG = np.random.default_rng(20260823)


@dataclass
class Result:
    game: str
    test: str
    scope: str
    statistic: float = float("nan")
    dof: float = float("nan")
    p_value: float = float("nan")
    n: int = 0
    effect: float = float("nan")
    detail: str = ""
    q_value: float = float("nan")
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# multiple-testing control
# ---------------------------------------------------------------------------
def benjamini_hochberg(pvals):
    """Return BH-adjusted q-values, same order as input."""
    p = np.asarray(pvals, dtype=float)
    ok = np.isfinite(p)
    q = np.full(p.shape, np.nan)
    if ok.sum() == 0:
        return q
    pv = p[ok]
    m = pv.size
    order = np.argsort(pv)
    ranked = pv[order]
    adj = ranked * m / (np.arange(m) + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(adj, 0, 1)
    q[ok] = out
    return q


# ---------------------------------------------------------------------------
# uniformity
# ---------------------------------------------------------------------------
def chi2_uniform(counts):
    """Chi-square goodness of fit against a uniform distribution."""
    c = np.asarray(counts, dtype=float)
    n = c.sum()
    k = c.size
    if n == 0 or k < 2:
        return float("nan"), float("nan"), float("nan")
    exp = n / k
    chi2 = float(((c - exp) ** 2 / exp).sum())
    dof = k - 1
    return chi2, dof, float(stats.chi2.sf(chi2, dof))


def chi2_mc_without_replacement(counts, n_draws, pool, k, sims=20000):
    """Monte-Carlo p-value for ball frequencies from k-of-pool draws.

    The analytic chi-square null is wrong here because the k numbers within one
    draw are mutually exclusive; simulating the real sampling scheme fixes it.
    """
    c = np.asarray(counts, dtype=float)
    exp = c.sum() / pool
    obs = float(((c - exp) ** 2 / exp).sum())

    sim_stats = np.empty(sims)
    for i in range(sims):
        picks = RNG.random((n_draws, pool)).argsort(axis=1)[:, :k]
        sim_counts = np.bincount(picks.ravel(), minlength=pool).astype(float)
        sim_stats[i] = ((sim_counts - exp) ** 2 / exp).sum()
    p = float((np.sum(sim_stats >= obs) + 1) / (sims + 1))
    return obs, p, sim_stats


def chi2_mc_fast(counts, n_draws, pool, k, sims=5000, max_cells=6_000_000):
    """Monte-Carlo p-value for ball frequencies, vectorised over simulations.

    `argpartition` is used rather than a full sort because only *which* k balls
    come out matters, not their order, and the batch size adapts so a 25-year
    archive does not blow up memory.
    """
    c = np.asarray(counts, dtype=float)
    exp = c.sum() / pool
    obs = float(((c - exp) ** 2 / exp).sum())
    if exp <= 0 or n_draws <= 0:
        return obs, float("nan")

    per_sim = n_draws * pool
    batch = max(1, min(sims, max_cells // max(per_sim, 1)))
    hits, done = 0, 0
    while done < sims:
        b = min(batch, sims - done)
        r = RNG.random((b, n_draws, pool))
        picks = np.argpartition(r, k - 1, axis=2)[:, :, :k] if k > 1 \
            else r.argmin(axis=2)[:, :, None]
        flat = picks.reshape(b, -1)
        sc = np.stack([np.bincount(row, minlength=pool) for row in flat]).astype(float)
        stat = ((sc - exp) ** 2 / exp).sum(axis=1)
        hits += int((stat >= obs).sum())
        done += b
    return obs, float((hits + 1) / (sims + 1))


def cramers_v(chi2, n, r, c):
    """Effect size for a contingency table."""
    if n <= 0 or min(r, c) < 2:
        return float("nan")
    return float(math.sqrt(chi2 / (n * (min(r, c) - 1))))


def contingency(table):
    t = np.asarray(table, dtype=float)
    t = t[t.sum(axis=1) > 0][:, t.sum(axis=0) > 0]
    if t.shape[0] < 2 or t.shape[1] < 2:
        return float("nan"), float("nan"), float("nan"), float("nan")
    chi2, p, dof, _ = stats.chi2_contingency(t)
    return float(chi2), float(dof), float(p), cramers_v(chi2, t.sum(), *t.shape)


# ---------------------------------------------------------------------------
# independence / sequence structure
# ---------------------------------------------------------------------------
def binom_test(successes, trials, p):
    if trials <= 0:
        return float("nan")
    return float(stats.binomtest(int(successes), int(trials), p).pvalue)


def runs_test(seq_binary):
    """Wald-Wolfowitz runs test for a 0/1 sequence."""
    x = np.asarray(seq_binary, dtype=int)
    n = x.size
    n1, n0 = int(x.sum()), int(n - x.sum())
    if n1 == 0 or n0 == 0 or n < 20:
        return float("nan"), float("nan")
    runs = 1 + int((x[1:] != x[:-1]).sum())
    mu = 2 * n1 * n0 / n + 1
    var = (2 * n1 * n0 * (2 * n1 * n0 - n)) / (n * n * (n - 1))
    if var <= 0:
        return float("nan"), float("nan")
    z = (runs - mu) / math.sqrt(var)
    return float(z), float(2 * stats.norm.sf(abs(z)))


def autocorr_permutation(series, lag, sims=5000):
    """Autocorrelation at `lag` with a permutation null (no distributional assumptions)."""
    x = np.asarray(series, dtype=float)
    n = x.size
    if n <= lag + 30:
        return float("nan"), float("nan")

    def ac(v):
        a, b = v[:-lag], v[lag:]
        sa, sb = a.std(), b.std()
        if sa == 0 or sb == 0:
            return 0.0
        return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))

    obs = ac(x)
    null = np.empty(sims)
    for i in range(sims):
        null[i] = ac(RNG.permutation(x))
    p = float((np.sum(np.abs(null) >= abs(obs)) + 1) / (sims + 1))
    return obs, p


def gap_test(positions, pool):
    """Compare observed gaps between recurrences of a value to Geometric(1/pool)."""
    g = np.asarray(positions, dtype=float)
    if g.size < 30:
        return float("nan"), float("nan"), float("nan")
    gaps = np.diff(g)
    if gaps.size < 30:
        return float("nan"), float("nan"), float("nan")
    mean_gap = float(gaps.mean())
    # Geometric with success prob 1/pool has mean `pool`
    ks = stats.kstest(gaps, lambda q: 1 - (1 - 1 / pool) ** np.floor(q))
    return mean_gap, float(ks.statistic), float(ks.pvalue)


def geometric_fit(run_lengths):
    """Fit Geometric(p) to rollover run lengths and test fit."""
    r = np.asarray(run_lengths, dtype=int)
    r = r[r >= 1]
    if r.size < 10:
        return float("nan"), float("nan"), float("nan"), 0
    phat = 1.0 / r.mean()
    kmax = int(r.max())
    obs = np.bincount(r, minlength=kmax + 1)[1:]
    ks = np.arange(1, kmax + 1)
    exp = r.size * phat * (1 - phat) ** (ks - 1)
    # pool the tail so every expected cell is >= 5
    keep, o, e = [], [], []
    acc_o = acc_e = 0.0
    for i in range(kmax):
        acc_o += obs[i]
        acc_e += exp[i]
        if acc_e >= 5:
            o.append(acc_o); e.append(acc_e); keep.append(ks[i]); acc_o = acc_e = 0.0
    if acc_e > 0 and e:
        o[-1] += acc_o; e[-1] += acc_e
    if len(o) < 3:
        return float(phat), float("nan"), float("nan"), int(r.size)
    o = np.array(o, dtype=float); e = np.array(e, dtype=float)
    e *= o.sum() / e.sum()
    chi2 = float(((o - e) ** 2 / e).sum())
    dof = len(o) - 2   # one parameter estimated
    p = float(stats.chi2.sf(chi2, dof)) if dof > 0 else float("nan")
    return float(phat), chi2, p, int(r.size)


def cusum_changepoint(binary_series):
    """Crude CUSUM change point on a 0/1 rate series; returns (index, max|S|)."""
    x = np.asarray(binary_series, dtype=float)
    if x.size < 50:
        return None, float("nan")
    s = np.cumsum(x - x.mean())
    i = int(np.argmax(np.abs(s)))
    return i, float(np.abs(s[i]) / math.sqrt(x.size))


def poisson_exact_p(observed, expected):
    """Two-sided exact Poisson p-value for a count vs expectation."""
    if expected <= 0:
        return float("nan")
    if observed >= expected:
        return float(min(1.0, 2 * stats.poisson.sf(observed - 1, expected)))
    return float(min(1.0, 2 * stats.poisson.cdf(observed, expected)))
