"""SQLite schema and upsert helpers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lotto.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS games (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    picks           INTEGER,
    pool_min        INTEGER,
    pool_max        INTEGER,
    bonus_name      TEXT,
    bonus_pool_min  INTEGER,
    bonus_pool_max  INTEGER,
    ordered         INTEGER DEFAULT 0,
    has_jackpot     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS draws (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game            TEXT NOT NULL REFERENCES games(code),
    draw_number     INTEGER,
    draw_date       TEXT,
    draw_time       TEXT,
    draw_period     TEXT,
    dow             INTEGER,
    numbers         TEXT,
    bonus_number    INTEGER,
    multiplier      TEXT,
    jackpot_cents   INTEGER,
    wins            INTEGER,
    promo           TEXT,
    source          TEXT NOT NULL,
    source_url      TEXT,
    raw             TEXT,
    fetched_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(game, draw_number, source)
);
CREATE INDEX IF NOT EXISTS idx_draws_game_date ON draws(game, draw_date);
CREATE INDEX IF NOT EXISTS idx_draws_game_num  ON draws(game, draw_number);

CREATE TABLE IF NOT EXISTS draw_numbers (
    draw_id     INTEGER NOT NULL REFERENCES draws(id) ON DELETE CASCADE,
    game        TEXT NOT NULL,
    draw_date   TEXT,
    position    INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    PRIMARY KEY (draw_id, position)
);
CREATE INDEX IF NOT EXISTS idx_dn_game_num ON draw_numbers(game, number);
CREATE INDEX IF NOT EXISTS idx_dn_game_date ON draw_numbers(game, draw_date);

CREATE TABLE IF NOT EXISTS jackpots (
    game            TEXT NOT NULL,
    draw_number     INTEGER NOT NULL,
    jackpot_cents   INTEGER,
    kind            TEXT,
    source          TEXT,
    PRIMARY KEY (game, draw_number, kind)
);

CREATE TABLE IF NOT EXISTS winners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game            TEXT NOT NULL,
    draw_number     INTEGER,
    draw_date       TEXT,
    draw_date_raw   TEXT,
    draw_time       TEXT,
    players         INTEGER,
    amount_cents    INTEGER,
    numbers         TEXT,
    location_raw    TEXT,
    source          TEXT,
    source_url      TEXT,
    match_method    TEXT,
    -- a plain UNIQUE over these columns does not work: draw_time and
    -- draw_date_raw are frequently NULL, and SQLite treats NULLs as distinct,
    -- so every re-scrape would insert the same announcements again
    fingerprint     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_winners_fp ON winners(fingerprint);

CREATE TABLE IF NOT EXISTS winner_locations (
    winner_id   INTEGER NOT NULL REFERENCES winners(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    outlet      TEXT,
    address     TEXT,
    area        TEXT,
    place       TEXT,      -- gazetteer match used for the map
    lat         REAL,
    lon         REAL,
    geo_method  TEXT,
    PRIMARY KEY (winner_id, seq)
);

CREATE TABLE IF NOT EXISTS inferred_wins (
    game            TEXT NOT NULL,
    draw_number     INTEGER NOT NULL,
    draw_date       TEXT,
    prev_jackpot    INTEGER,
    this_jackpot    INTEGER,
    next_jackpot    INTEGER,
    method          TEXT,
    confidence      REAL,
    agrees_with_published INTEGER,
    PRIMARY KEY (game, draw_number)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    target      TEXT,
    status      TEXT,
    rows        INTEGER,
    note        TEXT,
    ts          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts      TEXT,
    game        TEXT,
    test        TEXT,
    scope       TEXT,
    statistic   REAL,
    dof         REAL,
    p_value     REAL,
    q_value     REAL,
    n           INTEGER,
    effect      REAL,
    detail      TEXT
);
"""

# code, name, picks, pool_min, pool_max, bonus_name, bonus_min, bonus_max, ordered, has_jackpot
GAMES = [
    ("lotto",      "Lotto Plus",   5, 1, 36, "Power Ball", 1, 10, 0, 1),
    ("playwhe",    "Play Whe",     1, 1, 36, None, None, None, 0, 0),
    # Pick 2's `mega_ball` field is a 0/1 add-on flag, not a drawn ball
    ("pick2",      "Pick 2",       2, 1, 36, None, None, None, 1, 0),
    ("pick4",      "Pick 4",       4, 0, 9,  None, None, None, 1, 0),
    ("cashpot",    "Cash Pot",     5, 1, 20, None, None, None, 0, 1),
    ("winforlife", "Win For Life", 6, 1, 27, "Cash Ball", 1, 5, 0, 1),
    ("fastcash",   "Fast Cash",    1, 1, 36, None, None, None, 0, 1),
    # Play Whe and Fast Cash each run a second, multi-ball jackpot draw
    # alongside the headline single number; they are separate games statistically
    ("playwhe_jackpot",  "Play Whe Jackpot",  5, 1, 36, None, None, None, 0, 1),
    ("fastcash_jackpot", "Fast Cash Jackpot", 4, 1, 36, None, None, None, 0, 1),
]


def connect(path=None, timeout=60.0):
    # the scrapers run as separate processes and do write concurrently, so a
    # generous busy timeout is what keeps one from killing the other
    con = sqlite3.connect(path or DB_PATH, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(path=None):
    con = connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT OR IGNORE INTO games(code,name,picks,pool_min,pool_max,bonus_name,"
        "bonus_pool_min,bonus_pool_max,ordered,has_jackpot) VALUES (?,?,?,?,?,?,?,?,?,?)",
        GAMES,
    )
    con.commit()
    return con


DRAW_COLS = ("game", "draw_number", "draw_date", "draw_time", "draw_period", "dow",
             "numbers", "bonus_number", "multiplier", "jackpot_cents", "wins",
             "promo", "source", "source_url", "raw")

_UPDATE_COLS = [c for c in DRAW_COLS if c not in ("game", "draw_number", "source")]

_UPSERT_SQL = (
    "INSERT INTO draws (" + ",".join(DRAW_COLS) + ") "
    "VALUES (" + ",".join("?" * len(DRAW_COLS)) + ") "
    "ON CONFLICT(game, draw_number, source) DO UPDATE SET "
    + ",".join(c + "=COALESCE(excluded." + c + ", " + c + ")" for c in _UPDATE_COLS)
    + " RETURNING id"
)


def retry_locked(fn, *a, attempts=8, base=0.4, **kw):
    """Retry a write through SQLITE_BUSY.

    Several scrapers write concurrently. `busy_timeout` does not help when a
    connection has to upgrade a read transaction to a write one - SQLite returns
    BUSY straight away there to avoid deadlock - so the caller has to back off
    and try again.
    """
    import random
    import time
    last = None
    for i in range(attempts):
        try:
            return fn(*a, **kw)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e).lower():
                raise
            last = e
            if a and isinstance(a[0], sqlite3.Connection):
                try:
                    a[0].rollback()
                except Exception:                         # noqa: BLE001
                    pass
            time.sleep(base * (2 ** i) + random.random() * 0.3)
    raise last


def upsert_draw(con, rec):
    """Insert or update one draw. `rec` uses draws column names; `numbers` is a list."""
    return retry_locked(_upsert_draw, con, rec)


def _upsert_draw(con, rec):
    numbers = rec.get("numbers") or []
    vals = [rec.get(c) for c in DRAW_COLS]
    vals[DRAW_COLS.index("numbers")] = json.dumps(numbers)
    raw = rec.get("raw")
    if isinstance(raw, (dict, list)):
        vals[DRAW_COLS.index("raw")] = json.dumps(raw)

    row = con.execute(_UPSERT_SQL, vals).fetchone()
    draw_id = row[0] if row else None
    if draw_id is None:
        r = con.execute("SELECT id FROM draws WHERE game=? AND draw_number=? AND source=?",
                        (rec.get("game"), rec.get("draw_number"), rec.get("source"))).fetchone()
        draw_id = r[0] if r else None
    if draw_id is None:
        return None

    con.execute("DELETE FROM draw_numbers WHERE draw_id=?", (draw_id,))
    rows = [(draw_id, rec["game"], rec.get("draw_date"), i + 1, int(n))
            for i, n in enumerate(numbers) if n is not None]
    if rec.get("bonus_number") is not None:
        rows.append((draw_id, rec["game"], rec.get("draw_date"), 0, int(rec["bonus_number"])))
    if rows:
        con.executemany("INSERT OR REPLACE INTO draw_numbers "
                        "(draw_id,game,draw_date,position,number) VALUES (?,?,?,?,?)", rows)
    return draw_id


def log_ingest(con, source, target, status, rows=0, note=None):
    def _do():
        con.execute("INSERT INTO ingest_log(source,target,status,rows,note) VALUES (?,?,?,?,?)",
                    (source, target, status, rows, note))
        con.commit()
    retry_locked(_do)
