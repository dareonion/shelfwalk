"""SQLite persistence for Peoria Public Library catalog scrapes.

A growing time-series of what was on which branch's shelf, and when. Deliberately
generous with columns (the point is to keep more than we strictly need). Pure
stdlib `sqlite3`, no dependencies.

Two writers feed the same helpers:
  * library_lookup.py     — the live Playwright scraper (writes when --db is set)
  * ingest.py             — loads JSON captured via Claude-in-Chrome

Schema (all `CREATE TABLE IF NOT EXISTS`, so init is idempotent):
  titles            one row per catalog record (stable-ish metadata)
  scrapes           one row per lookup event
  availability      one row per branch copy per check  (the time-series core)
  search_snapshots  per-search Peoria-wide availability signal

Remote systems (the same want-list looked up at other libraries — see
bayarea_lookup.py):
  remote_bibs          which remote catalog record we matched each title to
  remote_availability  one row per branch copy per check, tagged with the system
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime

# --- branch normalization -------------------------------------------------------

# The six Peoria Public Library branches, as they appear after the "Peoria PL - "
# prefix in the catalog's Library column.
PEORIA_BRANCHES = ("Main St", "Lakeview", "Lincoln", "McClure", "North", "Outreach")


def branch_norm(library_text: str):
    """Normalize a Library-column value to (branch, is_peoria).

    Accepts either the full catalog form ('Peoria PL - North') or a bare branch
    name ('North') — the live scraper's DETAIL_JS already strips the prefix.
    Consortium libraries return (name, 0).
    """
    s = re.sub(r"\s+", " ", (library_text or "")).strip()
    m = re.match(r"^Peoria PL\s*-\s*(.+)$", s, re.IGNORECASE)
    if m:
        return m.group(1).strip(), 1
    if s in PEORIA_BRANCHES:
        return s, 1
    return s, 0


# --- classification (mirrors library_lookup.classify, kept standalone) ----------

_OUT_MARKERS = ("checked out", "transfer", "transit", "on hold", "in repair",
                "lost", "missing", "damaged", "claimed", "billed", "on order")
_NONCIRC_MARKERS = ("non-circulating", "workroom", "reference", "staff", "display")


def classify(status: str) -> str:
    s = (status or "").lower()
    if any(m in s for m in _OUT_MARKERS):
        return "out"
    if any(m in s for m in _NONCIRC_MARKERS):
        return "reference"
    return "available"


def format_guess(call_number: str, material_type: str = "", status: str = "") -> str:
    """Best-effort board/picture/reader/other from the cues the catalog exposes."""
    blob = " ".join((call_number or "", material_type or "", status or "")).lower()
    if "board" in blob or re.search(r"\bbb\b|/bbk|brdbk|bdbk|e brd", blob):
        return "board"
    if "reader" in blob:
        return "reader"
    if "picture" in blob or re.search(r"\bjp\b|\be(c|z)?\b|\bp\b", blob):
        return "picture"
    return "other"


# --- schema ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    record_id   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    author      TEXT,
    year        TEXT,
    isbns       TEXT,
    publisher   TEXT,
    phys_desc   TEXT,
    summary     TEXT,
    audience    TEXT,
    format      TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrapes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- 'search' | 'detail'
    query       TEXT,
    source      TEXT,                    -- 'library_lookup' | 'claude-in-chrome' | ...
    profile     TEXT
);

CREATE TABLE IF NOT EXISTS availability (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id     INTEGER REFERENCES scrapes(id),
    record_id     TEXT REFERENCES titles(record_id),
    title         TEXT,
    branch        TEXT NOT NULL,
    is_peoria     INTEGER NOT NULL,
    call_number   TEXT,
    material_type TEXT,
    status_raw    TEXT,
    state         TEXT,                  -- 'available' | 'out' | 'reference'
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id   INTEGER REFERENCES scrapes(id),
    record_id   TEXT REFERENCES titles(record_id),
    query       TEXT,
    call_number TEXT,
    local_count INTEGER,
    avail_text  TEXT,
    checked_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_avail_record  ON availability(record_id);
CREATE INDEX IF NOT EXISTS ix_avail_branch  ON availability(branch);
CREATE INDEX IF NOT EXISTS ix_avail_checked ON availability(checked_at);

CREATE TABLE IF NOT EXISTS remote_bibs (
    system      TEXT NOT NULL,            -- 'sccl' | 'sjpl' | 'mvpl'
    record_id   TEXT NOT NULL REFERENCES titles(record_id),
    bib_id      TEXT,                     -- remote catalog id; NULL = no match found
    title       TEXT,
    author      TEXT,
    format      TEXT,                     -- remote catalog's format label
    year        TEXT,
    match_score REAL,
    checked_at  TEXT NOT NULL,
    PRIMARY KEY (system, record_id)
);

CREATE TABLE IF NOT EXISTS raw_pages (
    url        TEXT PRIMARY KEY,       -- the exact request; newest fetch wins
    host       TEXT,
    body_gz    BLOB NOT NULL,          -- zlib-compressed response body
    nbytes     INTEGER,                -- uncompressed size
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_editions (
    system      TEXT NOT NULL,
    record_id   TEXT NOT NULL REFERENCES titles(record_id),
    bib_id      TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    format      TEXT,                     -- remote catalog's format label
    format_class TEXT,                    -- book | picture | board | audio | ebook | eaudio
    language    TEXT,                     -- 'eng' | 'spa' | 'chi' | … | NULL
    kind        TEXT,                     -- primary | edition | translation | audio
    match_score REAL,
    checked_at  TEXT NOT NULL,
    contents    TEXT,                     -- compilation contents note ('' = fetched, none)
    orig_title  TEXT,                     -- translation's original title ('' = fetched, none)
    details     TEXT,                     -- JSON: isbn/edition/publisher/phys_desc/
                                          -- summary/audience/series/subjects/genres/…
    PRIMARY KEY (system, record_id, bib_id)
);

CREATE TABLE IF NOT EXISTS remote_availability (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_id   INTEGER REFERENCES scrapes(id),
    system      TEXT NOT NULL,
    record_id   TEXT REFERENCES titles(record_id),
    bib_id      TEXT,
    title       TEXT,
    branch      TEXT NOT NULL,            -- branch (sccl/sjpl) or shelf location (mvpl)
    collection  TEXT,
    call_number TEXT,
    status_raw  TEXT,
    state       TEXT,                     -- 'available' | 'out' | 'reference'
    checked_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ravail_sys_rec ON remote_availability(system, record_id);
CREATE INDEX IF NOT EXISTS ix_ravail_checked ON remote_availability(checked_at);

-- Hot list (hotlist.py): a handful of watched new releases, polled far more
-- often than the want-list. Deliberately separate from titles/remote_bibs —
-- these are adult new releases matched by ISBN, not toddler picture books
-- matched by the fuzzy title rules, and they come and go within a season.
CREATE TABLE IF NOT EXISTS hot_titles (
    slug        TEXT PRIMARY KEY,        -- stable key; the watchlist file's join
    title       TEXT NOT NULL,
    author      TEXT,
    isbns       TEXT,                    -- comma-separated; the primary matcher
    pub_date    TEXT,                    -- expected publication, if known
    added_at    TEXT NOT NULL,
    retired_at  TEXT                     -- set when it drops off the watchlist
);

-- One row per (bib, check). The time series is the point: holds-per-copy is
-- what decides where to queue, and it moves by the hour around publication.
CREATE TABLE IF NOT EXISTS hot_sightings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    system      TEXT NOT NULL,
    bib_id      TEXT NOT NULL,
    title       TEXT,
    format      TEXT,
    format_class TEXT,                   -- 'print' | 'ebook' | 'audio' | 'other'
    holdable    INTEGER,
    status      TEXT,
    copies      INTEGER,
    on_order    INTEGER,
    available   INTEGER,
    holds       INTEGER,
    checked_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_hot_sight ON hot_sightings(slug, system, bib_id);
CREATE INDEX IF NOT EXISTS ix_hot_sight_when ON hot_sightings(checked_at);

-- The hold ledger, and the thing that stops a bug from placing a hold twice:
-- UNIQUE(slug, system, bib_id) means a re-run can never re-queue a bib we
-- already acted on, whatever the poller thinks it saw.
CREATE TABLE IF NOT EXISTS hot_holds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    system      TEXT NOT NULL,
    bib_id      TEXT NOT NULL,
    pickup      TEXT,
    outcome     TEXT NOT NULL,           -- 'placed'|'dry-run'|'failed'|'declined'
    detail      TEXT,
    holds_at_placement  INTEGER,
    copies_at_placement INTEGER,
    placed_at   TEXT NOT NULL,
    UNIQUE(slug, system, bib_id)
);

-- Acclaim (acclaim.py): awards, best-of lists and recommendation lists, kept
-- as a reference corpus independent of what any library happens to hold.
-- `works` is the spine; everything else hangs an accolade off a work_key.
CREATE TABLE IF NOT EXISTS works (
    work_key    TEXT PRIMARY KEY,        -- normalized 'title|surname'
    title       TEXT NOT NULL,
    subtitle    TEXT,
    author      TEXT,
    pub_year    TEXT,
    isbns       TEXT,
    wikidata_id TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accolades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_key    TEXT NOT NULL REFERENCES works(work_key),
    source      TEXT NOT NULL,           -- 'booker' | 'nyt-notable' | 'obama' ...
    source_kind TEXT NOT NULL,           -- 'award' | 'list' | 'popularity'
    category    TEXT,                    -- 'Fiction', 'General Nonfiction', ...
    year        INTEGER,
    status      TEXT NOT NULL,           -- winner|finalist|shortlist|longlist|listed
    detail      TEXT,
    url         TEXT,
    fetched_at  TEXT NOT NULL,
    UNIQUE(work_key, source, category, year, status)
);

CREATE INDEX IF NOT EXISTS ix_acc_work ON accolades(work_key);
CREATE INDEX IF NOT EXISTS ix_acc_source ON accolades(source, year);

-- Provenance: every pull logged, so a thin result is visibly a thin *fetch*
-- rather than a thin field. Without this a scraper that silently started
-- returning nothing looks exactly like an award that gave out no prizes.
-- A short work cannot be borrowed; the book that reprints it can. This is the
-- whole reason short fiction is tracked at all.
CREATE TABLE IF NOT EXISTS work_containers (
    work_key      TEXT NOT NULL,        -- the novella / story / poem
    container_key TEXT NOT NULL,        -- the collection or anthology
    source        TEXT NOT NULL,        -- 'isfdb' | 'marc505' | ...
    detail        TEXT,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (work_key, container_key, source)
);

-- Some awards attach to a person, not a book: the Nobel in Literature is for a
-- body of work, as are the SFWA Grand Master and most lifetime honours. Forcing
-- a laureate into `works` would invent a book that does not exist and inflate
-- every work-level score that counts distinct awards, so career awards live
-- here instead and are reported separately.
CREATE TABLE IF NOT EXISTS author_accolades (
    author      TEXT NOT NULL,
    author_key  TEXT NOT NULL,          -- normalized surname-ish, for joining
    source      TEXT NOT NULL,
    year        INTEGER,
    status      TEXT NOT NULL,
    detail      TEXT,
    url         TEXT,
    fetched_at  TEXT NOT NULL,
    UNIQUE(author_key, source, year, status)
);

-- Recomputed, never accumulated: rerunning the scorer must not drift.
CREATE TABLE IF NOT EXISTS work_scores (
    work_key    TEXT PRIMARY KEY,
    n_won       INTEGER,
    n_nominated INTEGER,
    n_lists     INTEGER,
    score       REAL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acclaim_fetches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    url         TEXT,
    ok          INTEGER NOT NULL,
    n_records   INTEGER,
    note        TEXT,
    fetched_at  TEXT NOT NULL
);
"""

_TITLE_FIELDS = ("author", "year", "isbns", "publisher", "phys_desc",
                 "summary", "audience", "format")


def open_db(path: str) -> sqlite3.Connection:
    """Connect, enable FKs, and ensure the schema exists (idempotent)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # the Bay Area lookup runs its systems in parallel threads, one connection
    # each: WAL lets readers pass the writer, and a generous busy timeout makes
    # concurrent writers queue instead of throwing 'database is locked'
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # light migration: columns added after the table first shipped
    cols = {r[1] for r in conn.execute("PRAGMA table_info(remote_editions)")}
    for col in ("contents", "orig_title", "details"):
        if col not in cols:
            conn.execute(f"ALTER TABLE remote_editions ADD COLUMN {col} TEXT")
    wcols = {r[1] for r in conn.execute("PRAGMA table_info(works)")}
    if "form" not in wcols:
        # novel|novella|novelette|short-story|poetry|drama|nonfiction|collection
        conn.execute("ALTER TABLE works ADD COLUMN form TEXT")
    conn.commit()
    return conn


# --- writes ---------------------------------------------------------------------

def record_scrape(conn, kind: str, checked_at: str, query: str = None,
                  source: str = None, profile: str = None) -> int:
    cur = conn.execute(
        "INSERT INTO scrapes (checked_at, kind, query, source, profile) "
        "VALUES (?,?,?,?,?)",
        (checked_at, kind, query, source, profile),
    )
    return cur.lastrowid


def upsert_title(conn, record_id: str, title: str, checked_at: str, meta: dict = None):
    """Insert a title or update its metadata + last_seen; keeps first_seen."""
    meta = meta or {}
    row = conn.execute(
        "SELECT record_id FROM titles WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        cols = ["record_id", "title", "first_seen", "last_seen"] + list(_TITLE_FIELDS)
        vals = [record_id, title, checked_at, checked_at] + [meta.get(f) for f in _TITLE_FIELDS]
        conn.execute(
            f"INSERT INTO titles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
    else:
        # Update title + any provided metadata fields, bump last_seen. COALESCE keeps
        # an existing value when this scrape didn't supply one.
        sets = ["title = ?", "last_seen = ?"]
        vals = [title, checked_at]
        for f in _TITLE_FIELDS:
            if meta.get(f) is not None:
                sets.append(f"{f} = ?")
                vals.append(meta[f])
        vals.append(record_id)
        conn.execute(f"UPDATE titles SET {', '.join(sets)} WHERE record_id = ?", vals)


def add_availability(conn, scrape_id: int, record_id: str, title: str,
                     holdings, checked_at: str):
    """holdings: iterable of dicts with branch/call_number/status/material_type."""
    for h in holdings:
        branch, is_peoria = branch_norm(h.get("branch", ""))
        status = (h.get("status") or "").strip()
        conn.execute(
            "INSERT INTO availability (scrape_id, record_id, title, branch, is_peoria, "
            "call_number, material_type, status_raw, state, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (scrape_id, record_id, title, branch, is_peoria,
             h.get("call_number"), h.get("material_type"), status,
             classify(status), checked_at),
        )


def upsert_remote_bib(conn, system: str, record_id: str, checked_at: str,
                      bib: dict = None, match_score: float = None):
    """Record which remote bib a title matched (bib=None → searched, nothing found)."""
    bib = bib or {}
    conn.execute(
        "INSERT OR REPLACE INTO remote_bibs (system, record_id, bib_id, title, "
        "author, format, year, match_score, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (system, record_id, bib.get("bib_id"), bib.get("title"), bib.get("author"),
         bib.get("format"), bib.get("year"), match_score, checked_at),
    )


def replace_remote_editions(conn, system: str, record_id: str, editions,
                            checked_at: str):
    """Replace the full edition set (all matched versions incl. the primary).

    A re-lookup supersedes the previous set wholesale, so a corrected match or a
    vanished edition can't leave a stale row behind.
    """
    conn.execute("DELETE FROM remote_editions WHERE system = ? AND record_id = ?",
                 (system, record_id))
    for e in editions:
        if not e.get("bib_id"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO remote_editions (system, record_id, bib_id, "
            "title, author, format, format_class, language, kind, match_score, "
            "checked_at, contents, orig_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (system, record_id, e["bib_id"], e.get("title"),
             ", ".join(e.get("authors") or []) or None, e.get("format"),
             e.get("format_class"), e.get("language"), e.get("kind"),
             e.get("match_score"), checked_at, e.get("contents"),
             e.get("orig_title")),
        )


def unenriched_editions(conn):
    """Every tracked edition whose record details aren't fetched yet."""
    return conn.execute(
        "SELECT system, record_id, bib_id, kind FROM remote_editions "
        "WHERE details IS NULL").fetchall()


def store_raw_page(conn, url: str, body: bytes, fetched_at: str):
    """Mirror a fetched response verbatim — every field the source sent stays
    re-parseable offline, so a parser fix never needs a re-scrape."""
    import urllib.parse
    import zlib
    conn.execute(
        "INSERT OR REPLACE INTO raw_pages (url, host, body_gz, nbytes, "
        "fetched_at) VALUES (?,?,?,?,?)",
        (url, urllib.parse.urlsplit(url).netloc, zlib.compress(body, 6),
         len(body), fetched_at))


def get_raw_page(conn, url: str) -> bytes | None:
    """The mirrored response body for a URL, decompressed."""
    import zlib
    row = conn.execute("SELECT body_gz FROM raw_pages WHERE url = ?",
                       (url,)).fetchone()
    return zlib.decompress(row["body_gz"]) if row else None


def delete_remote_edition(conn, system: str, record_id: str, bib_id: str):
    """Drop one tracked version (a verification failure); its availability
    rows stop being 'latest' the moment the edition row is gone."""
    conn.execute(
        "DELETE FROM remote_editions "
        "WHERE system = ? AND record_id = ? AND bib_id = ?",
        (system, record_id, bib_id))


def set_edition_details(conn, system: str, record_id: str, bib_id: str,
                        contents: str, orig_title: str, details: str = None):
    """'' / '{}' (not NULL) mark 'fetched, record has none' — no refetch."""
    conn.execute(
        "UPDATE remote_editions SET contents = ?, orig_title = ?, details = ? "
        "WHERE system = ? AND record_id = ? AND bib_id = ?",
        (contents or "", orig_title or "", details or "{}",
         system, record_id, bib_id))


def remote_editions(conn):
    """All current edition rows, for labeling availability in reports."""
    return conn.execute("SELECT * FROM remote_editions").fetchall()


def add_remote_availability(conn, scrape_id: int, system: str, record_id: str,
                            bib_id: str, title: str, items, checked_at: str):
    """items: iterable of dicts with branch/collection/call_number/status/state."""
    for it in items:
        conn.execute(
            "INSERT INTO remote_availability (scrape_id, system, record_id, bib_id, "
            "title, branch, collection, call_number, status_raw, state, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (scrape_id, system, record_id, bib_id, title,
             (it.get("branch") or "").strip() or "?", it.get("collection"),
             it.get("call_number"), it.get("status"), it.get("state"), checked_at),
        )


def add_search_snapshot(conn, scrape_id, record_id, query, call_number,
                        local_count, avail_text, checked_at):
    conn.execute(
        "INSERT INTO search_snapshots (scrape_id, record_id, query, call_number, "
        "local_count, avail_text, checked_at) VALUES (?,?,?,?,?,?,?)",
        (scrape_id, record_id, query, call_number, local_count, avail_text, checked_at),
    )


# --- reads ----------------------------------------------------------------------

def latest_availability(conn, is_peoria_only: bool = True):
    """Most-recent availability row per (record_id, branch). Returns sqlite3.Row list."""
    where = "WHERE is_peoria = 1" if is_peoria_only else ""
    return conn.execute(
        f"""
        SELECT a.* FROM availability a
        JOIN (
            SELECT record_id, branch, MAX(checked_at) AS mx
            FROM availability {where}
            GROUP BY record_id, branch
        ) last
        ON a.record_id = last.record_id AND a.branch = last.branch
           AND a.checked_at = last.mx
        """
    ).fetchall()


def latest_remote_availability(conn, system: str = None):
    """The current footprint of each title at each system.

    Rows come only from the newest scrape per (system, record_id) — an older
    scrape's branches must not linger once a re-scrape has replaced them — and
    only when they belong to a currently-matched bib (the primary in remote_bibs
    or any current remote_editions row), so availability recorded for a
    since-corrected mismatch disappears with the correction.
    """
    where = "WHERE system = ?" if system else ""
    args = (system,) if system else ()
    return conn.execute(
        f"""
        SELECT a.* FROM remote_availability a
        JOIN (
            SELECT system, record_id, MAX(checked_at) AS mx
            FROM remote_availability {where}
            GROUP BY system, record_id
        ) last
        ON a.system = last.system AND a.record_id = last.record_id
           AND a.checked_at = last.mx
        WHERE EXISTS (SELECT 1 FROM remote_bibs rb
                      WHERE rb.system = a.system AND rb.record_id = a.record_id
                        AND rb.bib_id = a.bib_id)
           OR EXISTS (SELECT 1 FROM remote_editions re
                      WHERE re.system = a.system AND re.record_id = a.record_id
                        AND re.bib_id = a.bib_id)
        """,
        args,
    ).fetchall()


def _now() -> str:
    """Timestamp in the same shape every other writer here uses."""
    return datetime.now().isoformat(timespec="seconds")


# --- hot list -------------------------------------------------------------------

def sync_hotlist(conn, entries) -> tuple[int, int]:
    """Upsert watchlist entries; retire rows whose slug has left the file.

    Retiring rather than deleting keeps the sighting history readable after a
    title is taken off the watch (and un-retires it if it comes back).
    """
    now = _now()
    live = set()
    added = 0
    for e in entries:
        live.add(e["slug"])
        cur = conn.execute("SELECT slug FROM hot_titles WHERE slug = ?",
                           (e["slug"],)).fetchone()
        if cur is None:
            added += 1
        conn.execute(
            "INSERT INTO hot_titles (slug, title, author, isbns, pub_date, "
            "added_at, retired_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, "
            "author=excluded.author, isbns=excluded.isbns, "
            "pub_date=excluded.pub_date, retired_at=NULL",
            (e["slug"], e["title"], e.get("author"),
             ",".join(e.get("isbns") or []), e.get("pub_date"), now))
    placeholders = ",".join("?" * len(live)) or "''"
    retired = conn.execute(
        f"UPDATE hot_titles SET retired_at = ? "
        f"WHERE retired_at IS NULL AND slug NOT IN ({placeholders})",
        (now, *live)).rowcount
    conn.commit()
    return added, retired


def add_hot_sighting(conn, slug: str, system: str, bib: dict, checked_at: str):
    conn.execute(
        "INSERT INTO hot_sightings (slug, system, bib_id, title, format, "
        "format_class, holdable, status, copies, on_order, available, holds, "
        "checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, system, bib["bib_id"], bib.get("title"), bib.get("format"),
         bib.get("format_class"), int(bool(bib.get("holdable"))),
         bib.get("status"), bib.get("copies"), bib.get("on_order"),
         bib.get("available"), bib.get("holds"), checked_at))


def previous_sighting(conn, slug: str, system: str, bib_id: str):
    """The most recent sighting of this bib *before* the current write."""
    return conn.execute(
        "SELECT * FROM hot_sightings WHERE slug = ? AND system = ? "
        "AND bib_id = ? ORDER BY checked_at DESC, id DESC LIMIT 1",
        (slug, system, bib_id)).fetchone()


def latest_hot_sightings(conn, slug: str = None):
    """Newest sighting per (slug, system, bib) — the current standing."""
    where = "WHERE slug = ?" if slug else ""
    args = (slug,) if slug else ()
    return conn.execute(
        f"""
        SELECT s.* FROM hot_sightings s
        JOIN (SELECT slug, system, bib_id, MAX(id) AS mx
              FROM hot_sightings {where} GROUP BY slug, system, bib_id) last
        ON s.id = last.mx
        ORDER BY s.slug, s.system, s.bib_id
        """, args).fetchall()


def hot_hold(conn, slug: str, system: str, bib_id: str):
    return conn.execute(
        "SELECT * FROM hot_holds WHERE slug = ? AND system = ? AND bib_id = ?",
        (slug, system, bib_id)).fetchone()


def hot_holds_for(conn, slug: str = None):
    where, args = ("WHERE slug = ?", (slug,)) if slug else ("", ())
    return conn.execute(
        f"SELECT * FROM hot_holds {where} ORDER BY placed_at DESC", args
    ).fetchall()


def record_hot_hold(conn, slug: str, system: str, bib_id: str, outcome: str,
                    pickup: str = None, detail: str = None,
                    holds: int = None, copies: int = None) -> bool:
    """Write the hold ledger row. Returns False if a *successful* one existed.

    The UNIQUE constraint is the guard rail, not the caller: whatever the
    poller decides, a bib already held is never held again. A previous
    'failed' row is the one thing that may be overwritten — a transient login
    or network error must not lock a title out of its queue for good.
    """
    # The DO UPDATE ... WHERE means a blocked conflict raises nothing and
    # simply writes no row, so rowcount — not the absence of an exception — is
    # what says whether this call actually did anything.
    cur = conn.execute(
        "INSERT INTO hot_holds (slug, system, bib_id, pickup, outcome, "
        "detail, holds_at_placement, copies_at_placement, placed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug, system, bib_id) DO UPDATE SET "
        "  outcome=excluded.outcome, detail=excluded.detail, "
        "  pickup=excluded.pickup, placed_at=excluded.placed_at, "
        "  holds_at_placement=excluded.holds_at_placement, "
        "  copies_at_placement=excluded.copies_at_placement "
        "WHERE hot_holds.outcome = 'failed'",
        (slug, system, bib_id, pickup, outcome, detail, holds, copies,
         _now()))
    conn.commit()
    return cur.rowcount > 0


def clear_hot_hold(conn, slug: str, system: str, bib_id: str) -> int:
    """Forget a ledger row so the bib becomes eligible again (a failed attempt
    we want to retry, or a hold cancelled by hand in the catalog)."""
    n = conn.execute(
        "DELETE FROM hot_holds WHERE slug = ? AND system = ? AND bib_id = ?",
        (slug, system, bib_id)).rowcount
    conn.commit()
    return n


# --- acclaim --------------------------------------------------------------------

def _fold(s: str) -> str:
    """Lowercase, strip accents, drop everything that is not a word character.

    ⚠ Uses \\w with the UNICODE flag, NOT [a-z0-9]. The first version of this
    stripped every non-ASCII character, so every CJK title normalized to the
    empty string and all 540 Douban books collapsed into 26 keys. Accents are
    folded first (NFKD + drop combining marks) so 'château' and 'chateau' still
    meet, while 九诗心 survives intact.
    """
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", s, flags=re.UNICODE).replace("_", "")


def work_key(title: str, author: str = None) -> str:
    """Stable join key: normalized title + author surname.

    Titles arrive punctuated differently from every source ('Life of M' vs
    'Life Of M: A Novel'), so the key drops the subtitle. Author is the surname
    only — sources disagree on 'Kuang, R. F.' vs 'R.F. Kuang'.
    """
    t = re.split(r"\s*[:;]\s*", (title or "").strip())[0]
    t = _fold(re.sub(r"^(the|a|an)\s+", "", t.strip().lower()))
    a = (author or "").strip()
    if "," in a:
        a = a.split(",")[0]
    else:
        a = a.split()[-1] if a.split() else ""
    return f"{t}|{_fold(a)}"


def upsert_work(conn, title: str, author: str = None, *, subtitle: str = None,
                pub_year: str = None, isbns=None, wikidata_id: str = None) -> str:
    key = work_key(title, author)
    now = _now()
    row = conn.execute("SELECT work_key FROM works WHERE work_key = ?",
                       (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO works (work_key, title, subtitle, author, pub_year, "
            "isbns, wikidata_id, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, title, subtitle, author, pub_year,
             ",".join(isbns or []), wikidata_id, now, now))
    else:
        # COALESCE so a later, richer source fills gaps without clobbering
        conn.execute(
            "UPDATE works SET last_seen = ?, subtitle = COALESCE(subtitle, ?), "
            "author = COALESCE(author, ?), pub_year = COALESCE(pub_year, ?), "
            "wikidata_id = COALESCE(wikidata_id, ?), "
            "isbns = CASE WHEN COALESCE(isbns,'') = '' THEN ? ELSE isbns END "
            "WHERE work_key = ?",
            (now, subtitle, author, pub_year, wikidata_id,
             ",".join(isbns or []), key))
    return key


def add_accolade(conn, work_key_: str, source: str, source_kind: str,
                 status: str, *, category: str = None, year: int = None,
                 detail: str = None, url: str = None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO accolades (work_key, source, source_kind, "
        "category, year, status, detail, url, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (work_key_, source, source_kind, category, year, status, detail, url,
         _now()))
    return cur.rowcount > 0


def log_fetch(conn, source: str, ok: bool, *, url: str = None,
              n_records: int = None, note: str = None) -> None:
    conn.execute(
        "INSERT INTO acclaim_fetches (source, url, ok, n_records, note, "
        "fetched_at) VALUES (?,?,?,?,?,?)",
        (source, url, int(bool(ok)), n_records, note, _now()))
    conn.commit()


def acclaim_stats(conn):
    return conn.execute(
        "SELECT source, status, COUNT(*) n, MIN(year) y0, MAX(year) y1 "
        "FROM accolades GROUP BY source, status ORDER BY source, status"
    ).fetchall()


def author_key(name: str) -> str:
    """Normalized author identity: lowercase, punctuation-free, diacritics kept
    as-is (sources agree on 'Krasznahorkai' far more than on initials)."""
    a = (name or "").strip()
    if "," in a:
        a = " ".join(reversed([p.strip() for p in a.split(",", 1)]))
    return re.sub(r"[^a-z0-9 ]+", "", a.lower()).strip()


def add_author_accolade(conn, author: str, source: str, status: str, *,
                        year: int = None, detail: str = None,
                        url: str = None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO author_accolades (author, author_key, source, "
        "year, status, detail, url, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        (author, author_key(author), source, year, status, detail, url, _now()))
    return cur.rowcount > 0
