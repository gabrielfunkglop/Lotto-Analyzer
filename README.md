# NLCB Lotto Analyzer

Scrapes every published Trinidad & Tobago NLCB draw into SQLite, works out which
draws were won and where the ticket was sold, runs a statistical battery looking
for anything that does not behave like a fair draw, and serves the lot as a local
web app with a map of the winning outlets.

```bash
python serve.py
```

Then open <http://127.0.0.1:5000>. The server is read-only, so it is safe to run
while a scrape is in progress.

| page | what it shows |
|---|---|
| **Overview** | coverage, the fairness verdict, latest draws |
| **Games** | per-game ball frequency with z-scores, pool eras, jackpot history, searchable draw table |
| **Fairness** | every test with its p and Benjamini-Hochberg q value |
| **Winners map** | winning outlets on a map of Trinidad, with Tobago inset |
| **Data quality** | source coverage, disagreements, missing draws, ingest log |

## Where the data comes from

| source | what it gives | depth |
|---|---|---|
| `nlcbgames.com` WordPress REST API | exact jackpot figures, draw times, authoritative recent results | ~1 year (Fast Cash ~6 months) |
| `nlcbgames.com/winners/` | prize amount, outlet name and address, draw number (Lotto) | ~66 announcements |
| `nlcbplaywhelotto.com` month archive | full draw history **and an explicit jackpot-winner count** | Play Whe / Pick 2 / Cash Pot to 2000, Lotto Plus to 2001, Pick 4 to 2012, Win For Life to 2022 |

The two draw sources are stored side by side, keyed `(game, draw_number, source)`,
never merged on write. That is deliberate: it makes disagreements visible, and the
analyzer reports them as a data-integrity finding rather than silently picking one.

Both sites need handling:

* `nlcbgames.com` sits behind a Sucuri javascript challenge. `lotto/http.py`
  decodes it and sets the cookie, so no browser is needed.
* `nlcbplaywhelotto.com` rate-limits by IP and starts silently dropping TCP
  connections after a short burst of POST searches, escalating if you keep
  knocking. The scraper crawls slowly, recycles its HTTP session (a long-lived one
  gets dropped even when a brand-new client from the same address connects fine)
  and waits out a block rather than retrying into it.

## Setup

```bash
pip install -r requirements.txt
```

## Ingest

```bash
python scrape.py nlcbgames --games all --include-fastcash
```

```bash
python scrape.py winners
```

```bash
python supervise.py --max-hours 14 --then-analyze
```

`supervise.py` drives the deep archive backfill: roughly 1,500 month queries at
~19 s each, so about 8-11 hours. It is fully resumable — every completed month is
recorded in `ingest_log` and every fetched page is cached under `data/cache/`, so
restarting picks up where it left off at no extra request cost.

Fast Cash has over 120,000 draws and plain offset pagination hits an undocumented
depth limit part-way through, so it is walked in date windows instead:

```bash
python scrape.py nlcbgames --games fastcash --windowed --since 2026-02-01 --window-days 3
```

## Daily refresh

```bash
python refresh.py
```

Pulls only the newest pages plus the last two months from the mirror (~40 requests),
then re-runs the analysis. Install it as a Windows scheduled task with:

```bash
python refresh.py --install-task --task-time 09:30
```

## Which draws were won

Two independent routes, which check each other:

1. **Published.** The archive mirror prints a `Wins` column for Lotto Plus, Cash
   Pot and Win For Life — the actual number of top-prize winners.
2. **Inferred.** A rolling jackpot only ever grows; it falls back to the seed the
   draw after somebody wins. So a fall between consecutive draws implies the
   earlier draw was won.

Scored against the published counts on Lotto Plus, the drop rule gets
**precision 0.90, recall 0.95**. Draws sitting at the advertised floor
(a flat $1,000,000 for years) are marked *undetermined* rather than guessed at —
a floor draw looks identical whether it was won or rolled, and scoring those as
negatives would flatter the rule.

The rule is only applied to games that actually roll. Cash Pot's top prize is
claimed on ~95% of draws: it pays out a pool rather than rolling a jackpot, so
rollover geometry and drop inference do not apply to it.

## Statistical approach

Roughly 150 tests run per full pass. At a 5% threshold you would expect several to
look "significant" on perfectly fair data, so:

* every p-value goes through **Benjamini-Hochberg**, and the report ranks on q;
* ball-frequency tests for multi-pick games use a **Monte-Carlo null that
  simulates real without-replacement draws** — the textbook chi-square null is
  wrong when the k numbers in a draw are mutually exclusive;
* carry-over tests use a **shuffled-order null**, because overlaps at a given lag
  share draws with their neighbours and the analytic variance is too small;
* number pools are **calibrated from the data**, using only values that recur, so
  one mistyped number cannot widen a pool;
* the history is **split at every pool change** and frequencies are never compared
  across one.

### Things that had to be discovered the hard way

Each of these produced a spectacular false positive before it was handled:

| discovery | effect if ignored |
|---|---|
| Cash Pot ran a **1–25** pool from 2007-09-26 to 2010-04-17, and 1–20 either side | balls 21–25 look ice-cold across the full archive |
| Lotto Plus dropped from **36 balls to 35** after 2012-09-01 | ball 36 looks ice-cold |
| Win For Life's cash ball runs **1–3**, not 1–5 | two impossible balls give p ≈ 3e-17 |
| Pick 2's `mega_ball` field is a **0/1 add-on flag**, not a drawn ball | p ≈ 6e-38 from testing a boolean for uniformity |
| The mirror writes `0-0-0-0-0` for draws it never obtained | hundreds of phantom draws of zeros |
| Pick 2 draw 5459 is published as "32 38" against a 1–36 pool | one typo widens the pool by two impossible balls |
| Cash Pot is won on ~95% of draws | rollover-geometry test returns p ≈ 5e-50 on a game that has no rollover |

Tests cover: ball and positional frequency, bonus-ball frequency, carry-over at
several lags, pair co-occurrence, consecutive numbers, repeated number sets, Pick 4
digit structure, number distribution conditioned on draw period / weekday / era,
rate change points, rollover run-length geometry, whether won draws skew toward
calendar-date numbers, out-of-pool values, cross-source agreement, winner-versus-draw
consistency, and winner-location concentration.

A flag means "this does not match the idealised fair model" — equally a data-quality
artefact, a rule change, or an equipment change. In this archive that has been the
cause every single time.

## The map

Winner cards give free-text addresses, never coordinates. `lotto/geo.py` matches
them offline against a gazetteer of ~170 Trinidad & Tobago towns and districts and
caches the result in the database, so the map needs no geocoding service and no
network at page-load time. The coastline is Natural Earth 10m, inlined at ~5 KB.

Markers sit at town centres, so the map shows the *area* a ticket was sold in, not
the exact shop. Two coastal towns (San Fernando, Carenage) are nudged ~0.5 km inland
because their true centres fall marginally outside that coastline.

## Layout

```
lotto/
  http.py               Sucuri challenge solving, retries, pacing, page cache
  db.py                 schema + upserts + SQLITE_BUSY retry
  normalize.py          date / time / money parsing across the source formats
  geo.py                Trinidad & Tobago gazetteer and address matching
  sources/archive.py    nlcbplaywhelotto.com month tables (all six games)
  sources/nlcbgames.py  official REST API + the four winner-card layouts
  analysis/core.py      statistical primitives, BH-FDR, Monte-Carlo nulls
  analysis/suite.py     the test battery
scrape.py               ingest CLI
supervise.py            restartable driver for the long backfill
refresh.py              daily incremental update + re-analysis
analyze.py              run the battery, write reports/report.md
serve.py                the web app
templates/ static/      web UI
data/lotto.db           SQLite output
data/tt_geom.json       coastline for the map
```

## Known limits

* Winner **location** data covers only the few dozen announcements the official
  site currently displays. It is not a complete record of wins and cannot support
  a geographic significance claim — the map is descriptive.
* The mirror leaves jackpot and win cells as `X` where it has no data, which is
  common for recent draws; the official API fills those in.
* Play Whe and Fast Cash each run a second multi-ball jackpot draw alongside the
  headline number; these are stored as separate games (`playwhe_jackpot`,
  `fastcash_jackpot`).
* Past draws carry no information about future ones. This is an audit tool, not a
  prediction tool.
