#!/usr/bin/env python3
"""Awards, best-of lists and canon — the CLI and the source registry.

The adapters live in `sources/`, one module per awarding body, and the shared
machinery (transports, the raw mirror, text repair, the yield guard) lives in
`acclaim_core`. Adding a source should mean writing one module and adding one
line to SOURCES below.

    uv run acclaim.py pull --source pulitzer      # one source
    uv run acclaim.py pull --all                  # every scriptable source
    uv run acclaim.py browser-plan                # what needs a Chrome pass
    uv run acclaim.py stats                       # coverage + provenance
    uv run acclaim.py score                       # rank books by breadth across juries
    uv run acclaim.py audio                       # rank audiobooks
    uv run acclaim.py shelf                       # acclaimed AND borrowable now
    uv run acclaim.py find "<story>"              # which book carries a work
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import catalog_db as db
from acclaim_core import (  # noqa: F401  (re-exported for callers and tests)
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, YIELD_DROP_THRESHOLD,
    YIELD_MIN_HISTORY, Source, _arm_archive, _decode_page, _fetch, _fetch_note,
    _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing, _wiki_plain,
    check_yield, demojibake, parse_credit)

from sources.pulitzer import *  # noqa: F401,F403
from sources.sfadb import *  # noqa: F401,F403
from sources.booker import *  # noqa: F401,F403
from sources.nba import *  # noqa: F401,F403
from sources.obama import *  # noqa: F401,F403
from sources.grammy import *  # noqa: F401,F403
from sources.audies import *  # noqa: F401,F403
from sources.kirkus import *  # noqa: F401,F403
from sources.womens_prize import *  # noqa: F401,F403
from sources.nyt import *  # noqa: F401,F403
from sources.wsj import *  # noqa: F401,F403
from sources.ft_business import *  # noqa: F401,F403
from sources.latimes import *  # noqa: F401,F403
from sources.douban import *  # noqa: F401,F403
from sources.pen import *  # noqa: F401,F403
from sources.goodreads import *  # noqa: F401,F403
from sources.nbcc import *  # noqa: F401,F403
from sources.nobel import *  # noqa: F401,F403
from sources.librofm import *  # noqa: F401,F403
from sources.apple_audio import *  # noqa: F401,F403
from sources.libby_audio import *  # noqa: F401,F403
from sources.audible import *  # noqa: F401,F403
from sources.listen_list import *  # noqa: F401,F403

# `import *` skips underscore names, but these are part of the module's
# de-facto surface (tests and callers reach for them), so re-export them.
from sources.goodreads import _gr_form  # noqa: F401
from sources.latimes import _lat_form  # noqa: F401
from sources.obama import _OBAMA_STOP_RE  # noqa: F401
from sources.sfadb import _load_sfadb, _sfadb_form  # noqa: F401

# --- registry -------------------------------------------------------------------

SOURCES: dict[str, Source] = {
    s.key: s for s in [
        Source("pulitzer", "Pulitzer Prize", "award", BROWSER, load_pulitzer,
               cadence="annual-may",
               note="6 book categories; TLS-fingerprint blocked, Chrome pass",
               forms=("novel", "drama", "nonfiction", "poetry")),
        Source("hugo", "Hugo Awards", "award", HTTP,
               lambda c: _load_sfadb(c, "hugo"), cadence="annual-aug",
               note="via sfadb (Locus Index to SF Awards)",
               forms=("novel", "novella", "novelette", "short-story")),
        Source("nebula", "Nebula Awards", "award", HTTP,
               lambda c: _load_sfadb(c, "nebula"), cadence="annual-may",
               note="via sfadb", forms=("novel", "novella", "novelette",
                                        "short-story")),
        Source("grammy", "Grammy Best Audio Book", "award", WIKIDATA,
               load_grammy, cadence="annual-feb",
               note="WIKIPEDIA FALLBACK: grammy.com blocks scripts and would "
                    "need a page per ceremony",
               forms=("audio",)),
        Source("audies", "Audie Awards (audiobooks)", "award", HTTP,
               load_audies, cadence="annual-mar",
               note="audiopub.org 1996-2026; records a narrator, not just an "
                    "author (theaudies.com is a parked domain)",
               forms=("audio",)),
        Source("kirkus", "Kirkus Prize", "award", HTTP, load_kirkus,
               cadence="annual-oct",
               note="one page per year since 2014; winners and finalists "
                    "are marked up differently",
               forms=("novel", "nonfiction")),
        Source("womens-prize", "Women's Prize", "award", HTTP,
               load_womens_prize, cadence="annual-jun",
               note="WP REST for the spine, book page for exact status",
               forms=("novel", "nonfiction")),
        Source("nyt", "New York Times book lists", "list", BROWSER, load_nyt,
               cadence="annual-nov",
               note="subscriber harvest: 10 Best, 100 Notable, 100 Best of the 21st C.",
               forms=("novel", "nonfiction", "poetry")),
        Source("wsj", "WSJ Best Books", "list", BROWSER, load_wsj,
               cadence="annual-dec", note="subscriber harvest",
               forms=("novel", "nonfiction")),
        Source("ft-business", "FT Business Book of the Year", "award",
               WIKIDATA, load_ft_business, cadence="annual-dec",
               note="WIKIPEDIA FALLBACK: FT's own shortlists live in article "
                    "prose that varies year to year; verified against ft.com",
               forms=("nonfiction",)),
        Source("latimes", "LA Times Book Prizes", "award", HTTP, load_latimes,
               cadence="annual-apr",
               note="history page, 1980 onward; winner ribbon is its own module",
               forms=("novel", "nonfiction", "poetry", "collection")),
        Source("douban", "Douban annual book lists", "list", BROWSER,
               load_douban, cadence="annual-dec",
               note="manual Chrome harvest: download blocked + PNA blocked",
               forms=("novel", "nonfiction", "poetry")),
        Source("pen", "PEN America Literary Awards", "award", BROWSER, load_pen,
               cadence="annual-mar",
               note="403s scripts; archive table paginates client-side, 1963-2023",
               forms=("novel", "nonfiction", "poetry", "drama")),
        Source("goodreads", "Goodreads Choice Awards", "award", HTTP,
               load_goodreads, cadence="annual-dec",
               note="popular vote, not a jury — 2009– , winner + 20 nominees",
               forms=("novel", "nonfiction", "poetry", "collection")),
        Source("nbcc", "National Book Critics Circle", "award", HTTP, load_nbcc,
               cadence="annual-mar",
               note="landing page carries the current cycle only; history TODO",
               forms=("novel", "nonfiction", "poetry")),
        Source("nobel", "Nobel Prize in Literature", "award", HTTP, load_nobel,
               cadence="annual-oct",
               note="official JSON API; author-level, writes author_accolades",
               forms=()),
        Source("obama", "Obama's reading lists", "list", HTTP, load_obama,
               cadence="annual-dec+summer",
               note="Medium 403s scripts; harvest/obama.json preferred, HTTP "
                    "fallback kept",
               forms=("novel", "nonfiction"), harvest=True),
        Source("nba", "National Book Awards", "award", HTTP, load_nba,
               cadence="annual-nov",
               note="all categories, discovered per year from the nav",
               forms=("novel", "nonfiction", "poetry")),
        Source("booker", "Booker Prize (all three)", "award", HTTP,
               load_booker, cadence="annual-nov",
               note="Booker + International + Children's, from the Booker Library",
               forms=("novel",)),
        Source("locus", "Locus Awards", "award", HTTP,
               lambda c: _load_sfadb(c, "locus"), cadence="annual-jun",
               note="via sfadb", forms=("novel", "novella", "novelette",
                                        "short-story", "collection",
                                        "anthology", "nonfiction")),
        Source("listen-list", "ALA RUSA Listen List", "list", HTTP,
               load_listen_list, cadence="annual-jan",
               note="outstanding audiobook narration, 2012–; rusaupdate.org "
                    "panels plus ALA pages for 2012–2015",
               forms=("audio",)),
        Source("audible-best", "Audible Best of the Year", "list", HTTP,
               load_audible_best, cadence="annual-nov",
               note="editors' Audiobook of the Year + Top 20, and genre lists "
                    "from the best-of-the-year blog tag",
               forms=("audio",)),
        Source("audible-charts", "Audible bestsellers", "popularity", HTTP,
               load_audible_charts, cadence="weekly",
               note="top 20 overall + 7 categories; robots.txt forbids paging",
               forms=("audio",)),
        Source("librofm", "Libro.fm bestsellers", "popularity", HTTP,
               load_librofm, cadence="weekly",
               note="site-wide top 100 audiobooks (indie bookstores); snapshot",
               forms=("audio",)),
        Source("apple-audio", "Apple Books top audiobooks", "popularity", HTTP,
               load_apple_audio, cadence="weekly",
               note="official marketing RSS, top 100 (US); kids' titles skipped; "
                    "no narrator",
               forms=("audio",)),
        Source("libby-audio", "OverDrive audiobook demand", "popularity", HTTP,
               load_libby_audio, cadence="weekly",
               note="Thunder API, top 300 adult English audiobooks by "
                    "OverDrive-wide popularity",
               forms=("audio",)),
    ]
}

# --- cli ------------------------------------------------------------------------

def cmd_pull(args) -> int:
    conn = db.open_db(args.db)
    keys = ([args.source] if args.source
            else [k for k, s in SOURCES.items()
                  if args.include_browser or s.transport != BROWSER])
    total = 0
    regressions: list = []
    for k in keys:
        src = SOURCES.get(k)
        if src is None:
            print(f"unknown source {k!r}; known: {', '.join(sorted(SOURCES))}",
                  file=sys.stderr)
            return 2
        try:
            n = src.load(conn)
            total += n
            print(f"{src.key:<14} +{n} accolades")
            # the yield check reads the row this load just wrote, so it sees
            # this run's parsed count against every previous one
            hist = db.past_yields(conn, src.key, limit=20)
            warn = check_yield(conn, src.key, hist[0] if hist else None) \
                if len(hist) > 1 else None
            if warn:
                print(f"  ! YIELD DROP  {warn}", file=sys.stderr)
                regressions.append(warn)
        except FileNotFoundError as exc:
            print(f"{src.key:<14} SKIPPED — {exc}", file=sys.stderr)
        except Exception as exc:                        # noqa: BLE001
            db.log_fetch(conn, src.key, False, note=f"{type(exc).__name__}: {exc}")
            print(f"{src.key:<14} FAILED — {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    print(f"total +{total}")
    if regressions:
        # exit non-zero so the timer logs a failed run, not a quiet one
        print(f"\n{len(regressions)} source(s) parsed far less than before:",
              file=sys.stderr)
        for w in regressions:
            print(f"  - {w}", file=sys.stderr)
        return 3
    return 0


def cmd_browser_plan(args) -> int:
    """What still needs a human-driven Chrome pass, and why."""
    print("These sources cannot run unattended:\n")
    for s in SOURCES.values():
        if s.transport != BROWSER and not s.harvest:
            continue
        # a harvest is either one file or a directory of per-list files
        one = os.path.join(HARVEST_DIR, f"{s.key}.json")
        many = os.path.join(HARVEST_DIR, s.key)
        have = os.path.exists(one) or (
            os.path.isdir(many) and any(f.endswith(".json") for f in os.listdir(many)))
        print(f"  {s.key:<14} {'harvested' if have else 'NOT HARVESTED':<14} {s.note}")
    print(f"\nHarvest files land in {HARVEST_DIR}/ via tools/collector.py.")
    print("See docs/harvesting.md for the per-source Chrome recipe.")
    return 0

# --- where a short work can actually be read -------------------------------------

# Much award short fiction first appeared in magazines with free online
# archives, so the first question is "is it a click away", not "which anthology
# carries it". The venue sfadb records is enough to tell.
FREE_ONLINE_VENUES = {
    "clarkesworld": "https://clarkesworldmagazine.com/",
    "uncanny": "https://www.uncannymagazine.com/",
    "lightspeed": "https://www.lightspeedmagazine.com/",
    "strange horizons": "http://strangehorizons.com/",
    "nightmare": "https://www.nightmare-magazine.com/",
    "apex": "https://apex-magazine.com/",
    "beneath ceaseless skies": "https://www.beneath-ceaseless-skies.com/",
    "tor.com": "https://reactormag.com/",
    "reactor": "https://reactormag.com/",
    "escape pod": "https://escapepod.org/",
    "diabolical plots": "https://www.diabolicalplots.com/",
    "khoreo": "https://www.khoreomag.com/",
    "khōréō": "https://www.khoreomag.com/",
    "fusion fragment": "https://www.fusionfragment.com/",
    "giganotosaurus": "https://giganotosaurus.org/",
    "fireside": "https://firesidefiction.com/",
    "podcastle": "https://podcastle.org/",
    "pseudopod": "https://pseudopod.org/",
    "translunar travelers lounge": "https://translunartravelerslounge.com/",
}
# Paid or print-only: a library copy or a subscription, not a click.
PRINT_MAGAZINES = ("asimov", "analog", "f&sf", "fantasy & science fiction",
                   "interzone", "black static", "weird tales")


def read_route(venue: str | None) -> dict:
    """-> {route, where} for a short work's original venue.

    route is 'free-online' | 'print-magazine' | 'book' | 'unknown'.
    """
    v = re.sub(r"\s+", " ", (venue or "")).strip()
    if not v:
        return {"route": "unknown", "where": None}
    low = v.lower()
    for name, url in FREE_ONLINE_VENUES.items():
        if low.startswith(name):
            return {"route": "free-online", "where": url}
    if any(low.startswith(p) for p in PRINT_MAGAZINES):
        return {"route": "print-magazine", "where": v}
    return {"route": "book", "where": v}

# --- ISFDB: which book contains a short work ------------------------------------

# You cannot borrow a novelette. ISFDB's title record lists every publication
# that carries a given piece of short fiction, which is exactly the mapping
# needed to turn "won the 2025 Hugo for Best Novelette" into "it's in this
# anthology, and Cupertino has a copy".
#
# On demand, not as a bulk crawl: the corpus holds thousands of short works and
# ISFDB is volunteer-run infrastructure. Two requests per lookup, cached in
# raw_pages like everything else.
ISFDB_SEARCH = ("https://www.isfdb.org/cgi-bin/se.cgi?arg={}"
                "&type=Fiction+Titles")
ISFDB_TITLE = "https://www.isfdb.org/cgi-bin/title.cgi?{}"
_ISFDB_TITLE_ID_RE = re.compile(r"title\.cgi\?(\d+)")
_ISFDB_PUB_RE = re.compile(r'pl\.cgi\?(\d+)"[^>]*>([^<]{2,120})')


def isfdb_containers(conn, title: str, author: str = None,
                     fails: list = None) -> list[dict]:
    """Publications carrying this short work.

    A publication whose title equals the work's is a standalone printing (Tor
    publishes novellas that way) and is still borrowable, so it is kept and
    flagged rather than dropped — the distinction that matters to a borrower
    is 'ask for this book', not 'is it an anthology'.
    """
    import urllib.parse
    fails = fails if fails is not None else []
    raw = _fetch(conn, ISFDB_SEARCH.format(urllib.parse.quote(title)), fails,
                 timeout=45)
    if raw is None:
        return []
    ids = _ISFDB_TITLE_ID_RE.findall(raw.decode("utf-8", "replace"))
    out, seen = [], set()
    for tid in ids[:3]:                     # a few variant title records at most
        traw = _fetch(conn, ISFDB_TITLE.format(tid), fails, timeout=45)
        if traw is None:
            continue
        page = traw.decode("utf-8", "replace")
        # search the whole page: the "Author:" line sits deep in the record
        if author and _flat_name(author) not in _flat_name(page):
            continue                        # a different work of the same name
        for pid, name in _ISFDB_PUB_RE.findall(page):
            name = _strip(name)
            if not name or pid in seen:
                continue
            seen.add(pid)
            out.append({"container": name, "isfdb_pub": pid,
                        "standalone": _flat_name(name) == _flat_name(title)})
    return out


def cmd_find(args) -> int:
    """Which borrowable book carries a given short work — and is it on a shelf."""
    conn = db.open_db(args.db)
    row = conn.execute(
        "SELECT * FROM works WHERE title = ? COLLATE NOCASE "
        "ORDER BY (form IN ('novella','novelette','short-story')) DESC LIMIT 1",
        (args.title,)).fetchone()
    author = args.author or (row["author"] if row else None)
    form = row["form"] if row else "?"
    print(f"{args.title}" + (f" — {author}" if author else "")
          + f"  [{form}]\n")
    if row is not None:
        for a in conn.execute(
                "SELECT source, category, year, status FROM accolades "
                "WHERE work_key = ? ORDER BY year DESC", (row["work_key"],)):
            print(f"  {a['year']}  {a['source']:<10} {a['status']:<10} "
                  f"{a['category'] or ''}")
        print()

    fails: list = []
    cons = isfdb_containers(conn, args.title, author, fails)
    if not cons:
        print("  no ISFDB publications found"
              + (f" ({len(fails)} fetch failures)" if fails else ""))
        return 0
    if row is not None:
        for c in cons:
            ck = db.upsert_work(conn, c["container"])
            conn.execute(
                "INSERT OR IGNORE INTO work_containers (work_key, container_key,"
                " source, detail, fetched_at) VALUES (?,?,?,?,?)",
                (row["work_key"], ck, "isfdb",
                 "standalone" if c["standalone"] else "anthology/collection",
                 db._now()))
        conn.commit()

    names = {}
    for c in cons:
        names.setdefault(c["container"], c["standalone"])
    print(f"  appears in {len(names)} publication(s):")
    import hotlist
    for name, standalone in list(names.items())[:args.limit]:
        tag = "standalone printing" if standalone else "anthology/collection"
        print(f"\n  · {name}   ({tag})")
        # the surname, not the whole credit: 'Ray Nayler' never appears inside
        # a catalog's 'Nayler, Ray'
        surnames = author_surnames(author)
        for sysname in ("sccl", "sjpl"):
            for cand in hotlist.bc_bibs(
                    sysname, {"title": name,
                              "author": surnames[0] if surnames else "",
                              "isbns": [], "formats": ("book",)}):
                if cand["format_class"] != "book":
                    continue
                print(f"      {sysname:<5} {cand['available'] or 0} on shelf / "
                      f"{cand['copies'] or 0} copies   {cand['url']}")
    return 0

# --- scoring --------------------------------------------------------------------

# Scores count distinct sources, not rows: Locus nominee lists run ten deep per
# category, so row counts would put a mid-list Locus nominee above a Pulitzer
# winner. Breadth across independent juries is the signal.
_WON = ("winner",)
_NOMINATED = ("finalist", "shortlist", "longlist", "nominee")
SCORE_WEIGHTS = {"won": 3.0, "nominated": 1.0, "listed": 2.0}

# Sources that vote on substantially the same ballot collapse to one family
# before counting; otherwise Hugo, Nebula and Locus let an SF novel bank three
# wins where a Pulitzer winner banks one.
AWARD_FAMILIES = {
    "hugo": "sf", "nebula": "sf", "locus": "sf",
    "booker": "booker", "booker-intl": "booker", "booker-childrens": "booker",
}


def award_family(source: str) -> str:
    return AWARD_FAMILIES.get(source, source)


# Audiobook sources judge or rank a recording, not the text, so they feed the
# audio ranking and never the book score. Goodreads and the LA Times are book
# sources except for their audiobook categories.
AUDIO_JURIES = {"audies", "grammy"}
AUDIO_LISTS = {"listen-list", "audible-best"}
AUDIO_POPULARITY = {"audible-charts", "librofm", "apple-audio", "libby-audio"}
AUDIO_SOURCES = AUDIO_JURIES | AUDIO_LISTS | AUDIO_POPULARITY
AUDIO_TOP_CATEGORIES = {("audies", "Audiobook Of The Year")}
# Juried audio categories inside book sources: they judge a recording too.
AUDIO_JURY_CATEGORIES = {("latimes", "Achievement In Audiobook Production")}
# Audie categories that judge packaging, not the listen.
AUDIO_IGNORED_CATEGORIES = {("audies", "Package Design"),
                            ("audies", "Excellence In Design")}
AUDIO_WEIGHTS = {"won": 3.0, "nominated": 1.0, "listed": 2.0, "popular": 1.0,
                 "top_prize": 2.0, "book": 0.5, "book_cap": 5.0}


def is_audio_accolade(source: str, category: str | None) -> bool:
    return (source in AUDIO_SOURCES or (source, category) in AUDIO_JURY_CATEGORIES
            or (source == "goodreads" and category == "Audiobook"))


def compute_scores(conn) -> int:
    """Recompute `work_scores` from scratch. Never incremental: a rerun after
    a parser fix must produce the same numbers, not accumulate on top."""
    conn.execute("DELETE FROM work_scores")
    # Aggregated in Python rather than SQL so the family collapse is visible
    # and testable; the table is small enough that it costs nothing.
    per_work: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT work_key, source, source_kind, status, category FROM accolades"):
        if is_audio_accolade(r["source"], r["category"]):
            continue
        acc = per_work.setdefault(r["work_key"],
                                  {"won": set(), "nom": set(), "lists": set()})
        fam = award_family(r["source"])
        if r["source_kind"] == "list":
            acc["lists"].add(fam)
        elif r["status"] in _WON:
            acc["won"].add(fam)
        elif r["status"] in _NOMINATED:
            acc["nom"].add(fam)
    now = db._now()
    for key, acc in per_work.items():
        # a family already counted as a win must not also count as a nomination
        nom = acc["nom"] - acc["won"]
        n_won, n_nom, n_lists = len(acc["won"]), len(nom), len(acc["lists"])
        score = (SCORE_WEIGHTS["won"] * n_won
                 + SCORE_WEIGHTS["nominated"] * n_nom
                 + SCORE_WEIGHTS["listed"] * n_lists)
        conn.execute(
            "INSERT INTO work_scores (work_key, n_won, n_nominated, n_lists, "
            "score, computed_at) VALUES (?,?,?,?,?,?)",
            (key, n_won, n_nom, n_lists, score, now))
    conn.commit()
    return len(per_work)


def compute_audio_scores(conn) -> int:
    """Recompute `audio_scores` from scratch, after `compute_scores`.

    score = 3 per audio jury family won + 1 per family nominated + 2 for a top
    category win + 2 per audio editorial list + 1 per popularity chart
    + half the book's own score, capped at 5. Only works with at least one
    audio signal are ranked; the book term orders recordings, never admits one.
    """
    conn.execute("DELETE FROM audio_scores")
    book = {r[0]: r[1] for r in conn.execute(
        "SELECT work_key, score FROM work_scores")}
    per_work: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT work_key, source, status, category, narrator FROM accolades"):
        src, cat = r["source"], r["category"]
        if not is_audio_accolade(src, cat) or (src, cat) in AUDIO_IGNORED_CATEGORIES:
            continue
        acc = per_work.setdefault(r["work_key"], {
            "won": set(), "nom": set(), "lists": set(), "pop": set(),
            "top": False, "narrators": {}})
        if src in AUDIO_JURIES or (src, cat) in AUDIO_JURY_CATEGORIES:
            fam = src if src in AUDIO_JURIES else f"{src}-audio"
            if r["status"] in _WON:
                acc["won"].add(fam)
                acc["top"] |= (src, cat) in AUDIO_TOP_CATEGORIES
            elif r["status"] in _NOMINATED:
                acc["nom"].add(fam)
        elif src in AUDIO_LISTS:
            acc["lists"].add(src)
        else:                                       # charts and the Goodreads vote
            acc["pop"].add("goodreads-audio" if src == "goodreads" else src)
        name = re.sub(r",?\s*published by\b.*$", "", r["narrator"] or "",
                      flags=re.I).strip(" ,")
        if name:
            acc["narrators"][name] = acc["narrators"].get(name, 0) + 1
    w = AUDIO_WEIGHTS
    now = db._now()
    for key, acc in per_work.items():
        nom = acc["nom"] - acc["won"]
        book_score = book.get(key) or 0.0
        score = (w["won"] * len(acc["won"]) + w["nominated"] * len(nom)
                 + w["top_prize"] * acc["top"] + w["listed"] * len(acc["lists"])
                 + w["popular"] * len(acc["pop"])
                 + min(w["book"] * book_score, w["book_cap"]))
        narrator = (max(acc["narrators"], key=lambda k: (acc["narrators"][k], -len(k)))
                    if acc["narrators"] else None)
        conn.execute(
            "INSERT INTO audio_scores (work_key, n_won, n_nominated, n_lists, "
            "n_popular, top_prize, book_score, score, narrator, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, len(acc["won"]), len(nom), len(acc["lists"]), len(acc["pop"]),
             int(acc["top"]), book_score, score, narrator, now))
    conn.commit()
    return len(per_work)


def cmd_audio(args) -> int:
    """The audiobook ranking, whether or not a copy is available."""
    conn = db.open_db(args.db)
    compute_scores(conn)
    n = compute_audio_scores(conn)
    print(f"ranked {n} audiobooks\n")
    print(f"{'score':>5}  {'won':>3} {'nom':>3} {'lst':>3} {'pop':>3} {'book':>4}  "
          f"title / author / narrator")
    for r in conn.execute(
            "SELECT s.*, w.title, w.author FROM audio_scores s "
            "JOIN works w USING(work_key) WHERE s.score >= ? "
            "ORDER BY s.score DESC, s.book_score DESC, w.title LIMIT ?",
            (args.min_score, args.top)):
        print(f"{r['score']:>5.1f}  {r['n_won']:>3} {r['n_nominated']:>3} "
              f"{r['n_lists']:>3} {r['n_popular']:>3} {r['book_score']:>4.0f}  "
              f"{r['title'][:48]} — {(r['author'] or '?')[:24]}"
              + (f" · read by {r['narrator'][:40]}" if r["narrator"] else ""))
    return 0


def cmd_score(args) -> int:
    conn = db.open_db(args.db)
    n = compute_scores(conn)
    print(f"scored {n} works\n")
    print(f"{'score':>6}  {'won':>3} {'nom':>3} {'lst':>3}  title")
    for r in conn.execute(
            "SELECT s.*, w.title, w.author, w.form FROM work_scores s "
            "JOIN works w USING(work_key) "
            "ORDER BY s.score DESC, w.title LIMIT ?", (args.top,)):
        who = (r["author"] or "")[:22]
        print(f"{r['score']:>6.1f}  {r['n_won']:>3} {r['n_nominated']:>3} "
              f"{r['n_lists']:>3}  {r['title'][:52]:<52} {who}")
    return 0

# --- the shelf join -------------------------------------------------------------

# The branches worth walking into. A system-wide "8 of 37 available" says
# nothing about the shelf you can reach, so branch_availability reports these
# only. Palo Alto (Mitchell Park) is a BiblioCommons instance used only here;
# Mountain View is out while bayarea_lookup.SKIPPED_SYSTEMS lists it.
FAVORITE_BRANCHES: dict[str, set | None] = {
    "sccl": {"Los Altos Library", "Cupertino Library"},
    "sjpl": {"Calabazas", "West Valley"},
    "paloalto": {"Mitchell Park"},
}
BC_SHELF_SYSTEMS = ("sccl", "sjpl", "paloalto")

_NAME_SUFFIX = re.compile(r"^(?:jr|sr|ii|iii|iv|md|phd)\.?$", re.I)


def author_surnames(author: str | None) -> list[str]:
    """Each credited author's surname, first author first.

    Corpus credits are 'First Last', with commas only between co-authors or
    before a suffix ('John Milton Cooper, Jr.'). Catalogs may credit only the
    first author (SCCL's print *Abundance* lists Klein alone), so searches use it.
    """
    out = []
    for name in re.split(r"\s*(?:&|;|,|\band\b)\s*", author or ""):
        words = [w for w in name.split() if not _NAME_SUFFIX.match(w)]
        if words:
            out.append(words[-1])
    return out


def _title_segments(title: str) -> set[str]:
    """Flattened pieces of a catalogued title: the stem, and each part a colon
    or comma sets off — so a volume filed under its series ('The Years of
    Lyndon Johnson: [Vol. 3], Master of the Senate') still offers its own name."""
    import hotlist
    t = re.sub(r"\[[^\]]*\]", " ", title or "")
    # colon parts keep their commas ('Sing, Unburied, Sing: A Novel' must still
    # offer 'Sing, Unburied, Sing'); comma parts are the series-volume case
    parts = [t] + re.split(r"\s*[:;=/]\s*", t) + re.split(r"\s*[:;,=/]\s*", t)
    out = set()
    for p in parts:
        p = re.sub(r"^\s*(?:the|a|an)\s+", "", p, flags=re.I)
        if hotlist._flat(p):
            out.add(hotlist._flat(p))
    return out


def shelf_match(title: str, author: str | None, cand: dict) -> bool:
    """Is this catalog record a print copy of this work?

    Stricter than hotlist._entry_matches, whose title containment suits a
    pre-publication 'Taipei Story (Deluxe Limited Edition)' but here would take
    Peter Robinson's 'Close to Home' for Marilynne Robinson's *Home*. So: a
    print format, English, the work's whole stem as the record's title or one
    of its parts, and the author among the record's credits.
    """
    import hotlist
    if cand.get("format_class") != "book":
        return False
    if (cand.get("language") or "eng") not in ("eng", "und"):
        return False
    # only the record is split into parts; the work's whole stem must be one
    # of them, or 'Sing, Unburied, Sing' would be satisfied by any 'Sing'
    want = hotlist._flat(re.sub(r"^\s*(?:the|a|an)\s+", "", shelf_stem(title),
                                flags=re.I))
    if not want or want not in _title_segments(cand.get("title")):
        return False
    credits = hotlist._flat(" ".join(str(a) for a in cand.get("authors") or []))
    surnames = [hotlist._flat(s) for s in author_surnames(author)]
    if credits and surnames:
        return any(s and s in credits for s in surnames)
    # Anthologies and poetry collections are often catalogued with no author at
    # all. With nothing to anchor on, only the whole title will do.
    ct = hotlist._flat(re.sub(r"\[[^\]]*\]", " ", cand.get("title") or ""))
    return ct in {hotlist._flat(title), hotlist._flat(shelf_stem(title))}


def shelf_candidates(system: str, title: str, author: str = None) -> list[dict]:
    """Print records of this work in one BiblioCommons system.

    Raises when the search itself failed: bc_bibs reports that as an empty
    list, and resolve_work_bibs would cache it as "this library holds nothing".
    """
    import hotlist
    surnames = author_surnames(author)
    entry = {"title": shelf_stem(title), "author": surnames[0] if surnames else "",
             "isbns": [], "formats": ("book",)}
    errors: list = []
    cands = hotlist.bc_bibs(system, entry, errors)
    if errors:
        raise RuntimeError("; ".join(errors))
    return [c for c in cands if shelf_match(title, author, c)]


def resolve_work_bibs(conn, work_key: str, title: str, author: str = None,
                      systems=BC_SHELF_SYSTEMS, refresh: bool = False) -> None:
    """Search each system ONCE and remember which record this work matches.

    The search is the expensive half — two queries per system per title — and
    it is also the stable half. Caching it turns the shelf join from minutes
    into one availability call per known bib. A system that holds nothing is
    recorded as a miss so it is not searched again. A re-search replaces that
    system's rows wholesale, so a corrected match leaves no stale bib behind.
    """
    already = set() if refresh else db.systems_searched(conn, work_key)
    for system in systems:
        if system in already:
            continue
        try:
            cands = shelf_candidates(system, title, author)
        except Exception:                                # noqa: BLE001
            continue                                     # leave unresolved
        db.clear_work_bibs(conn, work_key, system)
        if cands:
            for c in cands:
                db.record_work_bib(conn, work_key, system, c)
        else:
            db.record_work_bib(conn, work_key, system, None)   # recorded miss
        # commit before the next system's search: its response is mirrored
        # through a second connection, which an open write here would lock out
        conn.commit()


def branch_availability(conn, title: str, author: str = None,
                        work_key: str = None) -> list[dict]:
    """Per-branch copies of one work, restricted to FAVORITE_BRANCHES.

    Returns one row per (system, branch) with how many copies are on the shelf
    there right now, plus the system-wide hold queue for context. When a
    work_key is given the catalog match comes from work_bibs and only
    availability is fetched.
    """
    import bayarea_lookup as B
    import hotlist
    cached = {}
    if work_key:
        resolve_work_bibs(conn, work_key, title, author)
        for r in db.work_bibs(conn, work_key):
            if r["bib_id"]:
                cached.setdefault(r["system"], []).append(
                    {"bib_id": r["bib_id"], "title": r["title"],
                     "format_class": r["format_class"], "url": r["url"]})
    out = []
    for system in BC_SHELF_SYSTEMS:
        wanted = FAVORITE_BRANCHES.get(system)
        if work_key:
            cands = cached.get(system, [])
        else:
            try:
                cands = shelf_candidates(system, title, author)
            except Exception:                            # noqa: BLE001
                continue
        for c in cands:
            try:
                items = B.bc_parse_availability(json.loads(
                    B._get(f"{B.GATEWAY}/{system}/bibs/{c['bib_id']}/availability")))
            except Exception:                            # noqa: BLE001
                continue
            for branch in (wanted or {i["branch"] for i in items}):
                here = [i for i in items if i["branch"] == branch]
                if not here:
                    continue
                on_shelf = sum(1 for i in here if i["state"] == "available")
                out.append({"system": system, "branch": branch,
                            "on_shelf": on_shelf, "copies_here": len(here),
                            "holds": c.get("holds"), "sys_copies": c.get("copies"),
                            "url": c.get("url")})
    # Mountain View is one library on a WebPAC; its items carry shelf
    # locations rather than branches, so any available copy counts.
    if "mvpl" in B.SKIPPED_SYSTEMS:
        return out
    surnames = author_surnames(author)
    entry = {"title": shelf_stem(title), "author": surnames[0] if surnames else "",
             "isbns": [], "formats": ("book",)}
    try:
        for c in hotlist.mvpl_bibs(entry):
            if c["format_class"] != "book":
                continue
            out.append({"system": "mvpl", "branch": "Mountain View Public",
                        "on_shelf": c.get("available") or 0,
                        "copies_here": c.get("copies") or 0,
                        "holds": c.get("holds"), "sys_copies": c.get("copies"),
                        "url": c.get("url")})
    except Exception:                                    # noqa: BLE001
        pass
    return out


def shelf_stem(title: str) -> str:
    """The title before any subtitle or Booker marketing suffix. Search and
    match on this: a catalog's subtitle often differs from the corpus's."""
    t = re.split(r":\s*(?:Shortlisted|Longlisted|Winner|Nominated)\b", title or "")[0]
    return re.split(r"\s*[:;]\s*", t)[0].strip()


def cmd_shelf(args) -> int:
    """Acclaimed *and* borrowable: the top N scored works, matched live with
    shelf_candidates. Copy counts are system-wide, not per branch."""
    import hotlist
    conn = db.open_db(args.db)
    rows = conn.execute(
        "SELECT s.score, s.n_won, w.work_key, w.title, w.author, w.form "
        "FROM work_scores s JOIN works w USING(work_key) "
        "WHERE s.score >= ? AND (w.form IS NULL OR w.form IN "
        "  ('novel','nonfiction','collection','anthology')) "
        "ORDER BY s.score DESC, w.title LIMIT ?",
        (args.min_score, args.limit)).fetchall()
    if not rows:
        print("nothing scored yet — uv run acclaim.py score")
        return 0
    print(f"checking {len(rows)} works at {', '.join(args.system)}…\n")
    found = 0
    for r in rows:
        hits = []
        for sysname in args.system:
            try:
                hits += [(sysname, c) for c in
                         shelf_candidates(sysname, r["title"], r["author"])]
            except RuntimeError as exc:
                print(f"  ! {sysname}: {r['title'][:40]} — {exc}", file=sys.stderr)
        avail = [(s, c) for s, c in hits if (c.get("available") or 0) > 0]
        if not avail and not args.include_unavailable:
            continue
        found += 1
        print(f"{r['score']:>5.1f}  {r['title'][:52]:<52} {(r['author'] or '')[:24]}")
        for s, c in (avail or hits)[:3]:
            hpc = hotlist.holds_per_copy(c)
            print(f"        {s:<5} {c['available'] or 0:>3} on shelf / "
                  f"{c['copies'] or 0:>3} copies  "
                  f"{'' if hpc is None else f'{hpc:.2f} holds/copy'}  {c['url']}")
    print(f"\n{found} of {len(rows)} available now")
    return 0


def cmd_stats(args) -> int:
    conn = db.open_db(args.db)
    rows = db.acclaim_stats(conn)
    if not rows:
        print("nothing loaded yet — uv run acclaim.py pull --all")
        return 0
    print(f"{'source':<14} {'status':<10} {'n':>6}  years")
    for r in rows:
        span = (f"{r['y0']}–{r['y1']}" if r["y0"] and r["y1"] else "—")
        print(f"{r['source']:<14} {r['status']:<10} {r['n']:>6}  {span}")
    w = conn.execute("SELECT COUNT(*) c FROM works").fetchone()["c"]
    print(f"\n{w} distinct works")
    print("\nfetch log (most recent per source):")
    for r in conn.execute(
            "SELECT source, ok, n_records, note, fetched_at FROM acclaim_fetches f "
            "WHERE fetched_at = (SELECT MAX(fetched_at) FROM acclaim_fetches "
            "                    WHERE source = f.source) ORDER BY source"):
        print(f"  {r['source']:<14} {'ok ' if r['ok'] else 'FAIL'} "
              f"{str(r['n_records'] or ''):>6}  {r['fetched_at'][:16]}  "
              f"{(r['note'] or '')[:52]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="shelfwalk.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pull", help="load one source or every scriptable one")
    p.add_argument("--source")
    p.add_argument("--all", action="store_true",
                   help="every source whose transport runs unattended")
    p.add_argument("--include-browser", action="store_true",
                   help="also load browser-tier sources from their harvest files")

    sub.add_parser("browser-plan", help="what needs a Chrome pass")
    sub.add_parser("stats", help="coverage and provenance")

    sc = sub.add_parser("score", help="recompute work_scores across sources")
    sc.add_argument("--top", type=int, default=30)

    au = sub.add_parser("audio", help="rank audiobooks by juries, lists and charts")
    au.add_argument("--top", type=int, default=40)
    au.add_argument("--min-score", type=float, default=0.0)

    sh = sub.add_parser("shelf", help="acclaimed AND borrowable near you now")
    sh.add_argument("--limit", type=int, default=40,
                    help="how many top-scored works to check live (default 40)")
    sh.add_argument("--min-score", type=float, default=4.0)
    sh.add_argument("--system", action="append",
                    choices=list(BC_SHELF_SYSTEMS), default=None)
    sh.add_argument("--include-unavailable", action="store_true")

    fd = sub.add_parser("find", help="which book carries a short work")
    fd.add_argument("title")
    fd.add_argument("--author")
    fd.add_argument("--limit", type=int, default=6,
                    help="how many containers to check locally (default 6)")

    args = ap.parse_args(argv)
    if getattr(args, "system", None) is None and args.cmd == "shelf":
        args.system = ["sccl", "sjpl"]
    return {"pull": cmd_pull, "browser-plan": cmd_browser_plan,
            "stats": cmd_stats, "score": cmd_score, "audio": cmd_audio,
            "shelf": cmd_shelf, "find": cmd_find}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
