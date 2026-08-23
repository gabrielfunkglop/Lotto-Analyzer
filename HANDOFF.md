# Handoff

Everything you need to pick this project up cold. `README.md` is the user-facing
guide; this file is the "why is it like that" document.

---

## 1. Start here

```bash
pip install -r requirements.txt
python serve.py
```

`data/lotto.db` (~270 MB) is the whole product. If it is missing, rebuild it —
see §6. Nothing else holds state.

---

## 2. What each file is for

| file | responsibility |
|---|---|
| `lotto/http.py` | One `Fetcher` class: Sucuri challenge solving, retries, pacing, session recycling, on-disk page cache. **All** network access goes through it. |
| `lotto/db.py` | Schema (one `SCHEMA` string), game metadata table, `upsert_draw`, `retry_locked`. |
| `lotto/normalize.py` | Date/time/money parsing. The sources use three different date formats; this is where that mess is contained. |
| `lotto/geo.py` | Trinidad & Tobago gazetteer + address matching for the map. |
| `lotto/sources/archive.py` | `nlcbplaywhelotto.com` month-table scraper (all six games). |
| `lotto/sources/nlcbgames.py` | Official REST API + the four winner-card layouts. |
| `lotto/analysis/core.py` | Statistical primitives only — no domain knowledge. |
| `lotto/analysis/suite.py` | The test battery. Every test takes `(game, draws, meta)` and returns `Result` objects. |
| `scrape.py` | Ingest CLI. |
| `supervise.py` | Restartable driver for the multi-hour backfill. |
| `refresh.py` | Daily incremental update, installed as a scheduled task. |
| `analyze.py` | Runs the battery, applies FDR, writes `reports/report.md` and the `findings` table. |
| `serve.py` | Flask app. Read-only — it opens SQLite with `mode=ro`. |
| `templates/`, `static/` | Web UI. `_macros.html` holds shared fragments. |

---

## 3. Non-obvious things that will bite you

### The archive mirror bans you

`nlcbplaywhelotto.com` rate-limits by IP and silently **drops TCP connections**
after a short burst of POST searches. It escalates if you keep knocking, and a
*long-lived requests session gets dropped even when a brand-new client from the
same IP connects fine* — which is why `Fetcher._reset_session()` exists.

The backfill runs at ~19 s/request for a reason. Do not "optimise" it. Full
backfill is ~1,500 requests / 8–11 hours, and it is resumable: completed months
go in `ingest_log`, fetched pages are cached in `data/cache/`.

### Fast Cash cannot be paginated normally

Offset pagination on the official API hits an undocumented depth limit around
page 219. Use `iter_results_windowed()`, which walks by date instead:

```bash
python scrape.py nlcbgames --games fastcash --windowed --since 2026-02-01 --window-days 3
```

### Two sources are stored side by side, deliberately

Rows are keyed `(game, draw_number, source)`. They are **never merged on write**.
`suite.load_draws()` merges them at read time, preferring `nlcbgames` and filling
gaps from the mirror. This is what makes the "Data quality → Where the two
sources disagree" page possible, and that page is currently showing a real
finding (§5).

### SQLite writes need `retry_locked`

Several scrapers write concurrently. `busy_timeout` does *not* help when a
connection has to upgrade a read transaction to a write one — SQLite returns
BUSY immediately to avoid deadlock. Any new write path must go through
`db.retry_locked`.

### Winner rows are keyed on a fingerprint

The natural key contains nullable columns, and SQLite treats NULLs as distinct,
so a plain `UNIQUE` let every re-scrape triplicate the announcements. Hence
`winners.fingerprint` (sha1 of the content) with a unique index.
**If you add a field to a winner record, decide whether it belongs in the
fingerprint** — putting a volatile field in there will cause duplicates again.

---

## 4. The statistics, and how to not fool yourself

Roughly 150 tests run per pass. At a 5% threshold ~7 would look significant on
perfectly fair data, so **the bar is the Benjamini-Hochberg q-value, not p**.

Four rules the suite already enforces. Break any of them and you will produce a
confident, wrong headline:

1. **Pools come from the data**, using only values that recur ≥3 times
   (`supported_range`). One typo otherwise widens a pool by impossible balls.
2. **The history splits at every pool change** (`detect_pool_eras`), and
   frequency-type tests never compare across a boundary.
3. **Nulls must match the sampling scheme.** Multi-pick games use a Monte-Carlo
   null simulating without-replacement draws; carry-over tests use a
   shuffled-order null. The textbook chi-square null is wrong for both.
4. **Check the game actually works the way your model assumes.** Cash Pot's top
   prize is claimed on ~95% of draws — it is a pool, not a rollover.

Every one of these was learned by producing a false positive first:

| discovery | false result before it was handled |
|---|---|
| Cash Pot ran a 1–25 pool 2007-09-26 → 2010-04-17 | balls 21–25 ice-cold |
| Lotto Plus went 36 → 35 balls after 2012-09-01 | ball 36 ice-cold |
| Win For Life's cash ball is 1–3, not 1–5 | p ≈ 3e-17 |
| Pick 2's `mega_ball` is a 0/1 flag, not a ball | p ≈ 6e-38 |
| Cash Pot is won ~95% of draws | rollover geometry p ≈ 5e-50 |
| Pick 2 draw 5459 published as "32 38" (pool is 1–36) | pool widened by 2 impossible balls |
| The mirror writes `0-0-0-0-0` for draws it never got | hundreds of phantom draws of zeros |

### Adding a test

Write a function in `suite.py` taking `(game, draws, meta)` and returning a list
of `Result`. Then register it in `run_game`, in **one of two lists**:

- the plain list — for tests whose null does not depend on pool size;
- the `_era_slices` list — for anything pool-sensitive. It will be called once
  per era with a `meta` narrowed to that era, and the scope tagged automatically.

Return a `Result` with `p_value` unset for descriptive checks; they are excluded
from the FDR correction and shown separately.

---

## 5. Current state

- **Backfill complete.** 318k draw records, Jan 2000 – Aug 2026, all six archive
  games plus Fast Cash.
- **Draws look fair.** 156 tests, nothing survives FDR correction except two
  data-integrity findings.
- **Open finding, not yet resolved with NLCB:** `nlcbgames.com` publishes
  repeated number sets across distinct draws — Cash Pot Nov–Dec 2025 and Win For
  Life Dec 2025 – Jan 2026, 35 draws total. The mirror has distinct, plausible
  numbers for every one of them. Currently surfaced on the Data quality page and
  *not* silently corrected, because choosing a winner between two sources is a
  judgement call the tool should not make on its own. If you decide the mirror is
  authoritative for those draws, change `SOURCE_PRIORITY` in `suite.py` — but do
  it deliberately and document it.
- **Daily refresh** runs 09:30 via Windows Task Scheduler
  (`python refresh.py --install-task` to reinstall).

---

## 6. Rebuilding from nothing

```bash
python scrape.py nlcbgames --games all
```

```bash
python scrape.py nlcbgames --games fastcash --windowed --since 2026-02-01 --window-days 3
```

```bash
python scrape.py winners
```

```bash
python supervise.py --max-hours 14 --then-analyze
```

The last one is the 8–11 hour part. Everything else finishes in minutes.

---

## 7. Front-end conventions

- **Table headers stick to `.tablewrap`, never the viewport.** Sticking them to
  the viewport requires the site header's exact height as a magic offset; when
  that drifted by 4px, rows scrolled through the gap. Add `.tall` to a
  `.tablewrap` to cap its height and engage the sticky header.
- **Colour only via tokens on `:root`.** The dark palette is declared twice —
  under `prefers-color-scheme` and under `[data-theme="dark"]` — so the toggle
  wins in both directions. Never give a colour its only definition inside a
  media query.
- **All ten pages pass WCAG AA in both themes.** If you add a muted-text colour,
  re-check it; `--ink-3` is already at the edge, and on tinted backgrounds you
  need `--ink-2`.
- **No inline styles in templates.** Layout helpers live at the bottom of
  `style.css`. Repeated markup goes in `templates/_macros.html`.
- No CDN, no external fonts, no tile server. The map is inlined Natural Earth
  geometry (~5 KB) projected in `serve.py`; it works offline.

---

## 8. Things worth doing next

1. **Winner location data is thin** — 66 announcements, ~102 mapped outlets. It
   cannot support any geographic claim, and the UI says so. If NLCB ever exposes
   a fuller archive, the map becomes genuinely analytical rather than
   illustrative.
2. **`playwhe_jackpot` only goes back to Jan 2026** because the official API is
   the only source for it. The mirror does not publish those draws.
3. **Report the duplicate-records finding to NLCB.** It is a real defect in their
   published data and the evidence is clean.
4. **`analyze.py:write_report` is 86 lines** of linear string building. It works,
   but it is the least pleasant function in the codebase to modify.
5. There is no test suite. The verification so far has been empirical (route
   smoke tests, contrast audits, point-in-polygon checks on the map). A handful
   of unit tests around `normalize.py` and `detect_pool_eras` would be the
   highest-value place to start.
