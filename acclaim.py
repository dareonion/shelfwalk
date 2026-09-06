#!/usr/bin/env python3
"""Awards, best-of lists and canon — a sourced corpus, joined to the shelf.

The want-list answers "is it on a shelf this morning"; `hotlist.py` answers
"where will I be in the queue". This answers the question before both of them:
*what is worth reading*, from the bodies that decide it — and then hands the
answer to the library machinery to find a copy.

Design notes that are load-bearing:

  * **Original sources, Wikipedia only as fallback.** Measured 2026-09-06:
    Wikidata carries 818 Booker nominees but only 21 Pulitzer finalists (there
    are ~200), 6 Women's Prize nominees and 1 for the NBCC. Fine for winners,
    useless for the shortlists — which is most of what makes a corpus worth
    having. Anything sourced only from Wikidata is flagged as such.
  * **Three transports.** Most sites yield to plain HTTP. A few defeat scripted
    clients entirely — pulitzer.org 403s `urllib` *and* `curl`, which is TLS
    fingerprinting, not headers — and those are fetched by JavaScript running
    inside a real Chrome tab, which POSTs its harvest to `tools/collector.py`.
  * **Raw first.** Scripted fetches mirror into `raw_pages` before parsing, and
    browser harvests land as JSON in `harvest/`. A parser fix must never cost a
    re-crawl; for the browser tier, where re-crawling means driving Chrome by
    hand, that matters far more than usual.

    uv run acclaim.py pull --source pulitzer      # one source
    uv run acclaim.py pull --all                  # every scriptable source
    uv run acclaim.py browser-plan                # what needs a Chrome pass
    uv run acclaim.py stats                       # coverage + provenance
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from html import unescape as htmlunescape
from typing import Callable

import catalog_db as db

HARVEST_DIR = "harvest"

# Transports, in order of preference.
HTTP = "http"            # scriptable; mirrored through bayarea_lookup._get
BROWSER = "browser"      # needs a Chrome pass (bot-walled or paywalled)
WIKIDATA = "wikidata"    # fallback and cross-check only


@dataclass
class Source:
    """One place accolades come from.

    Adding a source should mean writing one parser and registering it. If an
    adapter needs more than that, the framework is wrong, not the source.
    """
    key: str
    label: str
    kind: str                       # 'award' | 'list' | 'popularity'
    transport: str
    load: Callable                  # (conn) -> int records written
    cadence: str = "annual"
    note: str = ""
    forms: tuple = field(default_factory=lambda: ("book",))


# --- fetching -------------------------------------------------------------------

def _fetch(conn, url: str, fails: list, *, accept: str = "text/html",
           timeout: float = 60) -> bytes | None:
    """Mirror-first fetch. Returns the body, or None with the reason recorded.

    Every loader here used to `except Exception: continue`, and that is exactly
    how a Booker backfill quietly fetched 5 pages out of 733: a concurrent run
    held the SQLite write lock, `_get` archives *inside* its own try, and
    'database is locked' came back through the same `except Exception` as a
    404 would. 728 silent failures were indistinguishable from 728 books with
    no prize history.

    So: failures are counted and surfaced in the fetch log, never swallowed.
    A source that suddenly returns nothing must look different from a source
    that has nothing to return.
    """
    raw = db.get_raw_page(conn, url)
    if raw is not None:
        return raw
    import bayarea_lookup as B
    # Arm the mirror here rather than in each loader. Three sources had already
    # been written without calling set_archive, and the only symptom was zero
    # rows in raw_pages — the pull still "worked", so the raw-first guarantee
    # was quietly not holding for them.
    _arm_archive(conn)
    try:
        return B._get(url, accept=accept, timeout=timeout)
    except Exception as exc:                             # noqa: BLE001
        fails.append(f"{url}: {type(exc).__name__}: {exc}")
        return None


def _arm_archive(conn) -> None:
    import bayarea_lookup as B
    if getattr(B, "_archive_path", None):
        return
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if path:
        B.set_archive(path)


def _fetch_note(ok_count: int, fails: list, what: str) -> str:
    """One line summarising a pull, including how it failed if it did."""
    note = f"{ok_count} {what}"
    if fails:
        note += f"; {len(fails)} fetch failures, first: {fails[0][:120]}"
    return note


def _warn_if_mostly_failing(source: str, ok_count: int, fails: list) -> None:
    if fails and ok_count == 0:
        print(f"  ! {source}: every fetch failed ({len(fails)}). "
              f"First: {fails[0][:160]}", file=sys.stderr)
    elif len(fails) > ok_count:
        print(f"  ! {source}: {len(fails)} failures vs {ok_count} successes — "
              f"first: {fails[0][:160]}", file=sys.stderr)


# --- text repair ----------------------------------------------------------------

# Some sources serve double-encoded UTF-8: a curly apostrophe (U+2019, bytes
# e2 80 99) comes back as the three characters those bytes name in Latin-1.
# PEN does this on 5 of its 999 rows, which is exactly the frequency that
# survives a spot-check and then quietly poisons a work_key.
#
# Round-tripping through Latin-1 undoes it. The guard is that the round trip
# must SUCCEED and yield different text: genuine Latin-1 text like 'château'
# fails to decode as UTF-8 and is returned untouched.
_MOJI_LEAD = re.compile("[ÂÃâã]")


def demojibake(s: str) -> str:
    if not s or not _MOJI_LEAD.search(s):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    # a repair that introduces control characters is not a repair
    if fixed == s or any(ord(c) < 32 and c not in "\t\n" for c in fixed):
        return s
    return fixed


# --- credit parsing -------------------------------------------------------------

# 'Angel Down, by Daniel Kraus (Atria Books)' and its many near-misses. The
# publisher is optional (Drama entries carry none), and titles routinely
# contain commas — 'The Netanyahus: An Account of a Minor and Ultimately Even
# Negligible Episode…, by Joshua Cohen' — so the author split anchors on the
# LAST ', by ', not the first.
_BY_RE = re.compile(r",\s+by\s+", re.I)
_PUB_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def parse_credit(raw: str) -> dict:
    """'<Title>, by <Author> (<Publisher>)' -> dict, tolerantly.

    Returns title/author/publisher with author and publisher possibly None.
    'No award' and similar non-entries return an empty title, which callers
    skip — a year with no prize is data, not a parse failure.
    """
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s or re.fullmatch(r"no award(ed)?\.?", s, re.I):
        return {"title": "", "author": None, "publisher": None}

    publisher = None
    m = _PUB_RE.search(s)
    if m:
        # only strip a trailing paren group if it looks like a publisher rather
        # than part of the title ('(Deluxe Limited Edition)' stays)
        inner = m.group(1).strip()
        if inner and not re.search(r"\b(edition|vol\.?|volume|reprint)\b", inner, re.I):
            publisher = inner
            s = s[:m.start()].strip()

    parts = _BY_RE.split(s)
    if len(parts) >= 2:
        title = _BY_RE.split(s)[0] if len(parts) == 2 else ", by ".join(parts[:-1])
        author = parts[-1].strip()
    else:
        title, author = s, None
    return {"title": title.strip().strip(","),
            "author": (author or None), "publisher": publisher}


# --- Pulitzer -------------------------------------------------------------------

# Which Pulitzer category maps to which written form. Drama is a play; the
# nonfiction categories are books; Poetry is a collection, not a single poem.
_PULITZER_FORMS = {
    "Fiction": "novel", "Drama": "drama", "History": "nonfiction",
    "Biography": "nonfiction", "General Nonfiction": "nonfiction",
    "Poetry": "poetry",
}


def load_pulitzer(conn) -> int:
    """Load `harvest/pulitzer.json`, produced by the Chrome pass.

    pulitzer.org fingerprint-blocks scripted clients, so the fetch is done by
    JavaScript in a real tab (see `docs/harvesting.md`); this half is pure
    parsing and runs offline against the harvested file.
    """
    path = os.path.join(HARVEST_DIR, "pulitzer.json")
    if not os.path.exists(path):
        db.log_fetch(conn, "pulitzer", False, note=f"{path} missing — Chrome pass needed")
        raise FileNotFoundError(
            f"{path} not found. Run the Chrome harvest first: "
            "uv run acclaim.py browser-plan")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)

    n = 0
    for r in rows:
        credit = parse_credit(demojibake(r.get("raw", "")))
        if not credit["title"]:
            continue                      # 'No award' years
        key = db.upsert_work(conn, credit["title"], credit["author"])
        if _PULITZER_FORMS.get(r.get("category")):
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (_PULITZER_FORMS[r["category"]], key))
        if db.add_accolade(conn, key, "pulitzer", "award", r["status"],
                           category=r.get("category"), year=r.get("year"),
                           detail=(r.get("citation") or None) or credit["publisher"],
                           url=r.get("person_url")):
            n += 1
    conn.commit()
    db.log_fetch(conn, "pulitzer", True, url="https://www.pulitzer.org/",
                 n_records=n, note=f"{len(rows)} raw rows from harvest")
    return n


# --- sfadb (Hugo / Nebula / Locus, incl. short fiction) -------------------------

# sfadb is the Locus Index to SF Awards: one site with complete winner AND
# nominee lists for all three awards, every category, back to each award's
# first year. The official sites are patchier and three separate scrapes.
SFADB_AWARDS = {
    "hugo":   ("Hugo Awards", "Hugo_Awards", 1953),
    "nebula": ("Nebula Awards", "Nebula_Awards", 1966),
    "locus":  ("Locus Awards", "Locus_Awards", 1971),
}

# Only written work. The dropped categories are people and productions —
# Best Editor, Best Dramatic Presentation, Best Fan Artist — which are real
# awards but not things you can borrow.
_SFADB_FORMS = {
    "novel": "novel", "first novel": "novel", "novella": "novella",
    "novelette": "novelette", "short story": "short-story",
    "short fiction": "short-story", "collection": "collection",
    "anthology": "anthology", "poem": "poetry", "long poem": "poetry",
    "short poem": "poetry", "nonfiction": "nonfiction",
    "non-fiction": "nonfiction", "related work": "nonfiction",
    "related book": "nonfiction", "young adult book": "novel",
    "ya book": "novel", "horror novel": "novel", "fantasy novel": "novel",
    "science fiction novel": "novel", "graphic story": "collection",
    "novelette/novella": "novella",
}

_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_CATBLOCK_SPLIT = '<div class="categoryblock">'
_CAT_RE = re.compile(r'<div class="category">(.*?)</div>', re.S | re.I)


def _sfadb_form(category: str) -> str | None:
    c = re.sub(r"\s+", " ", (category or "").strip().lower())
    c = re.sub(r"^best\s+", "", c)
    for suffix in (" (tie)", ":"):
        c = c.replace(suffix, "")
    return _SFADB_FORMS.get(c.strip())


def parse_sfadb_year(page: str) -> list[dict]:
    """One sfadb year page -> entries.

    Winners carry `<span class="winner">`; everything else in the same list is
    a nominee. Title is the `<b>`, author the first `<a>`, publisher the
    trailing parenthetical.
    """
    out = []
    for chunk in page.split(_CATBLOCK_SPLIT)[1:]:
        m = _CAT_RE.search(chunk)
        if not m:
            continue
        category = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        form = _sfadb_form(category)
        if form is None:
            continue                       # people and productions
        body = chunk[m.end():]
        for li in _LI_RE.findall(body):
            is_winner = 'class="winner"' in li
            tm = re.search(r"<b>(.*?)</b>", li, re.S)
            if not tm:
                continue
            title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
            am = re.search(r"<a\b[^>]*>(.*?)</a>", li, re.S)
            author = re.sub(r"<[^>]+>", "", am.group(1)).strip() if am else None
            tail = re.sub(r"<[^>]+>", "", li[tm.end():])
            pm = re.search(r"\(([^()]*)\)\s*$", tail.strip())
            out.append({"category": category, "form": form, "title": title,
                        "author": author,
                        "publisher": pm.group(1).strip() if pm else None,
                        "status": "winner" if is_winner else "nominee"})
    return out


def _load_sfadb(conn, award_key: str) -> int:
    import bayarea_lookup as B
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    _label, slug, first_year = SFADB_AWARDS[award_key]
    import datetime
    this_year = datetime.date.today().year
    n = years_ok = 0
    fails: list = []
    for year in range(first_year, this_year + 1):
        url = f"https://www.sfadb.com/{slug}_{year}"
        raw = _fetch(conn, url, fails)
        if raw is None:
            continue                                    # award not held / no page
        page = raw.decode("utf-8", "replace")
        entries = parse_sfadb_year(page)
        if entries:
            years_ok += 1
        for e in entries:
            key = db.upsert_work(conn, e["title"], e["author"])
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (e["form"], key))
            if db.add_accolade(conn, key, award_key, "award", e["status"],
                               category=e["category"], year=year,
                               detail=e["publisher"], url=url):
                n += 1
        conn.commit()
    _warn_if_mostly_failing(award_key, years_ok, fails)
    db.log_fetch(conn, award_key, years_ok > 0, url=f"https://www.sfadb.com/{slug}",
                 n_records=n, note=_fetch_note(years_ok, fails, "years from sfadb"))
    return n


# --- Booker (+ International, + Children's) -------------------------------------

# Each book in the Booker Library carries its own prize history as dt/dd pairs:
#   <dt class="h6">Winner</dt>
#   <dd><a href="/the-booker-library/prize-years/1969">The Booker Prize 1969</a></dd>
# One page can hold several (longlisted, then shortlisted, then winner), and
# the prize name inside the <dd> is what separates Booker from International
# Booker — so status and award both come from the same pair.
_BOOKER_SITEMAPS = [
    f"https://thebookerprizes.com/sitemaps/default/sitemap.xml?page={p}"
    for p in (1, 2)
]
_DTDD_RE = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S | re.I)
_PRIZE_YEAR_RE = re.compile(
    r"(The Booker Prize|The International Booker Prize|"
    r"The Children's Booker Prize|Booker Prize|International Booker Prize)"
    r"\s*(\d{4})", re.I)
_BOOKER_STATUS = {
    "winner": "winner", "shortlisted": "shortlist", "longlisted": "longlist",
    "special award": "special", "finalist": "shortlist",
}
_BOOKER_KEYS = {
    "the booker prize": "booker", "booker prize": "booker",
    "the international booker prize": "booker-intl",
    "international booker prize": "booker-intl",
    "the children's booker prize": "booker-childrens",
}


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def parse_booker_book(page: str, url: str = "") -> dict:
    """One Booker Library book page -> {title, author, accolades:[…]}."""
    tm = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    title = _strip(tm.group(1)).split("|")[0].strip() if tm else ""
    # the author's own name is the first <h2> that is not a section heading
    author = None
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", page, re.S | re.I):
        t = _strip(m.group(1))
        if t and t.lower() not in ("buy the book", "features", "related",
                                   "you might also like", "the booker library"):
            author = t
            break
    out = []
    for dt, dd in _DTDD_RE.findall(page):
        status = _BOOKER_STATUS.get(_strip(dt).lower())
        if not status:
            continue
        pm = _PRIZE_YEAR_RE.search(_strip(dd))
        if not pm:
            continue
        award = _BOOKER_KEYS.get(pm.group(1).strip().lower(), "booker")
        out.append({"award": award, "status": status, "year": int(pm.group(2))})
    return {"title": title, "author": author, "accolades": out, "url": url}


def _booker_book_urls(conn) -> list[str]:
    import bayarea_lookup as B
    urls = []
    for sm in _BOOKER_SITEMAPS:
        raw = db.get_raw_page(conn, sm) or B._get(sm, accept="application/xml",
                                                  timeout=60)
        urls += re.findall(r"<loc>([^<]+)</loc>", raw.decode("utf-8", "replace"))
    return sorted({u for u in urls if "/the-booker-library/books/" in u})


def load_booker(conn) -> int:
    import bayarea_lookup as B
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    books = _booker_book_urls(conn)
    n = pages = 0
    fails: list = []
    for url in books:
        raw = _fetch(conn, url, fails)
        if raw is None:
            continue
        rec = parse_booker_book(raw.decode("utf-8", "replace"), url)
        if not rec["title"] or not rec["accolades"]:
            continue
        pages += 1
        key = db.upsert_work(conn, rec["title"], rec["author"])
        conn.execute("UPDATE works SET form = COALESCE(form, 'novel') "
                     "WHERE work_key = ?", (key,))
        for a in rec["accolades"]:
            if db.add_accolade(conn, key, a["award"], "award", a["status"],
                               category="Fiction", year=a["year"], url=url):
                n += 1
        # Commit before the next fetch, always. `_archive` mirrors through its
        # OWN connection, so an uncommitted write transaction here blocks it —
        # one process deadlocking itself on SQLite's single writer. `_get`
        # then reads 'database is locked' as a fetch failure and retries, and
        # the backfill crawls to a halt after the first handful of pages.
        conn.commit()
    conn.commit()
    _warn_if_mostly_failing("booker", pages, fails)
    db.log_fetch(conn, "booker", pages > 0,
                 url="https://thebookerprizes.com/the-booker-library",
                 n_records=n,
                 note=_fetch_note(pages, fails, f"book pages of {len(books)}"))
    return n


# --- National Book Awards -------------------------------------------------------

# nationalbook.org puts one category per request:
#   /awards-prizes/national-book-awards-<year>/?cat=<slug>
# and marks status by container, not by text — <div class="winner-book">,
# "finalist-books", "long-list" — so the section is what says winner vs
# finalist. Categories are read from each year's own nav rather than hardcoded,
# because they changed: Translated Literature only exists from 2018.
_NBA_SECTIONS = (("winner-book", "winner"), ("finalist-books", "finalist"),
                 ("long-list", "longlist"))
_NBA_CAT_RE = re.compile(
    r'class="category-winner[^"]*"\s+href="[^"]*\?cat=([a-z0-9-]+)"[^>]*>\s*([^<]+)')
_NBA_ENTRY_RE = re.compile(
    r"<h1[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</h1>\s*<h2[^>]*>(.*?)</h2>",
    re.S | re.I)
_NBA_FORMS = {"fiction": "novel", "nonfiction": "nonfiction",
              "poetry": "poetry", "translated-literature": "novel",
              "ypl": "novel", "young-peoples-literature": "novel"}


def parse_nba_page(page: str) -> list[dict]:
    """One (year, category) page -> entries tagged by their section."""
    out = []
    for cls, status in _NBA_SECTIONS:
        for m in re.finditer(rf'<div class="{cls}"[^>]*>', page):
            # to the start of the next section div, or end of document
            rest = page[m.end():]
            nxt = min([p for p in
                       (rest.find(f'<div class="{c}"') for c, _ in _NBA_SECTIONS)
                       if p != -1] or [len(rest)])
            for t, a in _NBA_ENTRY_RE.findall(rest[:nxt]):
                title, author = _strip(t), _strip(a)
                if title and author:
                    out.append({"title": title, "author": author,
                                "status": status})
    return out


def load_nba(conn) -> int:
    import bayarea_lookup as B
    import datetime
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    base = "https://www.nationalbook.org/awards-prizes/national-book-awards-"
    n = years_ok = 0
    fails: list = []
    for year in range(1950, datetime.date.today().year + 1):
        root = f"{base}{year}/"
        raw = _fetch(conn, root, fails)
        if raw is None:
            continue                                     # no award that year
        page = raw.decode("utf-8", "replace")
        cats = _NBA_CAT_RE.findall(page) or [("fiction", "Fiction")]
        got = False
        for slug, label in cats:
            url = f"{root}?cat={slug}"
            craw = _fetch(conn, url, fails)
            if craw is None:
                continue
            for e in parse_nba_page(craw.decode("utf-8", "replace")):
                got = True
                key = db.upsert_work(conn, e["title"], e["author"])
                conn.execute(
                    "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                    (_NBA_FORMS.get(slug, "novel"), key))
                if db.add_accolade(conn, key, "nba", "award", e["status"],
                                   category=_strip(label), year=year, url=url):
                    n += 1
            conn.commit()      # before the next category's fetch — see load_booker
        years_ok += bool(got)
        conn.commit()
    _warn_if_mostly_failing("nba", years_ok, fails)
    db.log_fetch(conn, "nba", years_ok > 0,
                 url="https://www.nationalbook.org/", n_records=n,
                 note=_fetch_note(years_ok, fails, "years parsed"))
    return n


# --- Obama's reading lists ------------------------------------------------------

# The lists live on his Medium, in plain prose: a 'Favorite Books of <year>'
# heading, then one book per line as 'Title — Author'. The Obama Foundation
# page (obama.org/stories/favorites-<year>) renders the same list from
# JavaScript, so Medium is the scriptable original.
#
# The RSS feed carries only the ~10 most recent posts, which is enough to keep
# up going forward but not to backfill; older post URLs carry an opaque hash
# and are listed in OBAMA_SEED_POSTS as they are found.
OBAMA_FEED = "https://barackobama.medium.com/feed"
OBAMA_SEED_POSTS = [
    "https://barackobama.medium.com/"
    "here-are-my-favorite-books-movies-and-music-of-2025-7139a0bdaf5b",
    "https://barackobama.medium.com/"
    "my-2026-summer-reading-music-lists-44a3366666b4",
]
_OBAMA_TITLE_RE = re.compile(
    r"(favorite books|summer reading|reading list|books.*of \d{4})", re.I)
# Where the books stop. The year-end post says "Favorite Movies of <year>";
# the summer post says "Summer Playlist:" — a heading the first pass did not
# know about, which let 46 songs through as books. So: any short heading that
# names music or film ends the book section.
_OBAMA_STOP_RE = re.compile(
    r"^\s*(?:my\s+)?(?:\d{4}\s+)?[\w' &-]{0,30}?"
    r"\b(playlist|music|songs?|movies?|films?|tv|television|podcasts?)\b"
    r"[\w' &-]{0,20}:?\s*$", re.I)
# 'Paper Girl — Beth Macy', 'The Look by Michelle Obama'
_OBAMA_ENTRY_RE = re.compile(
    r"^\s*(.+?)\s+(?:—|–|--|\bby\b)\s+([A-Z][^—–]{2,60})\s*$")


def parse_obama_post(html: str) -> list[dict]:
    """Medium post -> [{title, author}], from the books sections only."""
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&amp;", "&", text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    out, collecting, seen = [], False, set()
    for ln in lines:
        if not ln:
            continue
        if re.search(r"favorite books|books i recommended|summer reading", ln, re.I):
            collecting = True
            continue
        if collecting and _OBAMA_STOP_RE.match(ln):
            collecting = False
            continue
        if not collecting or len(ln) > 110:
            continue
        m = _OBAMA_ENTRY_RE.match(ln)
        if not m:
            continue
        title, author = m.group(1).strip(" .,"), m.group(2).strip(" .,")
        if len(title) < 2 or title.lower().startswith(("press enter", "and obviously")):
            continue
        if (title, author) not in seen:
            seen.add((title, author))
            out.append({"title": title, "author": author})
    return out


def _obama_post_urls(conn) -> list[str]:
    import bayarea_lookup as B
    urls = list(OBAMA_SEED_POSTS)
    try:
        feed = B._get(OBAMA_FEED, accept="application/rss+xml",
                      timeout=45).decode("utf-8", "replace")
        for title, link in re.findall(
                r"<title><!\[CDATA\[(.*?)\]\]></title>.*?<link>(.*?)</link>",
                feed, re.S):
            if _OBAMA_TITLE_RE.search(title):
                urls.append(link.split("?")[0])
    except Exception:                                    # noqa: BLE001
        pass                                             # seeds still work
    return sorted(set(urls))


def load_obama(conn) -> int:
    """Medium 403s scripted clients persistently — not a transient rate limit —
    so a browser harvest at harvest/obama.json wins when present. The HTTP path
    stays as the route that works whenever Medium relents."""
    path = os.path.join(HARVEST_DIR, "obama.json")
    if os.path.exists(path):
        return _load_obama_harvest(conn, path)
    import bayarea_lookup as B
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    n = posts = 0
    fails: list = []
    for url in _obama_post_urls(conn):
        raw = _fetch(conn, url, fails)
        if raw is None:
            continue
        html = raw.decode("utf-8", "replace")
        ym = re.search(r"(?:of|list[s]?)[- ](\d{4})", url) or re.search(r"(\d{4})", url)
        year = int(ym.group(1)) if ym else None
        entries = parse_obama_post(html)
        if entries:
            posts += 1
        for e in entries:
            key = db.upsert_work(conn, e["title"], e["author"])
            if db.add_accolade(conn, key, "obama", "list", "listed",
                               category="Obama's favorites", year=year,
                               url=url):
                n += 1
        conn.commit()
    _warn_if_mostly_failing("obama", posts, fails)
    db.log_fetch(conn, "obama", posts > 0, url=OBAMA_FEED, n_records=n,
                 note=_fetch_note(posts, fails, "posts parsed"))
    return n


# --- New York Times lists -------------------------------------------------------

# Harvested in a Chrome tab against Darren's own subscription. The interactive
# lists render client-side and the site is hostile to scripted clients, so the
# page does the reading and the result lands in harvest/nyt/<slug>.json as
#   {list, year, books: [{rank, title, author, year}]}
#
# Two shapes bit while extracting the 100 Best Books of the 21st Century, and
# both are the same lesson — a fixed line offset is a guess about layout:
#   * six titles wrap onto two lines, putting the byline a line further down;
#   * #88 "The Collected Stories of Lydia Davis" carries its author inside the
#     title, so its byline line is a bare year.
NYT_DIR = os.path.join(HARVEST_DIR, "nyt")
_NYT_TRANSLATOR_RE = re.compile(r"\s*[;,]\s*(translated|edited|with)\b.*$", re.I)
_NYT_LABELS = {
    "nyt-100-21c": "100 Best Books of the 21st Century",
    "nyt-10-best": "The 10 Best Books",
    "nyt-notable": "100 Notable Books",
}


def clean_nyt_author(name: str) -> str:
    """'Elena Ferrante; translated by Ann Goldstein' -> 'Elena Ferrante'."""
    return _NYT_TRANSLATOR_RE.sub("", demojibake(name or "")).strip(" ,;")


def load_nyt(conn) -> int:
    if not os.path.isdir(NYT_DIR):
        db.log_fetch(conn, "nyt", False,
                     note=f"{NYT_DIR}/ missing — Chrome pass needed")
        raise FileNotFoundError(f"{NYT_DIR}/<slug>.json not found. "
                                "See docs/harvesting.md.")
    n = files = 0
    for fn in sorted(os.listdir(NYT_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(NYT_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        slug = payload.get("list") or fn[:-5]
        label = _NYT_LABELS.get(slug, slug)
        listed_year = payload.get("year")
        listed_year = int(listed_year) if str(listed_year).isdigit() else None
        files += 1
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            key = db.upsert_work(conn, title, clean_nyt_author(b.get("author")) or None,
                                 pub_year=str(b["year"]) if b.get("year") else None)
            rank = b.get("rank")
            if db.add_accolade(conn, key, "nyt", "list", "listed",
                               category=label, year=listed_year,
                               detail=(f"rank {rank}" if rank else None),
                               url="https://www.nytimes.com/books/"):
                n += 1
        conn.commit()
    db.log_fetch(conn, "nyt", files > 0, url="https://www.nytimes.com/books/",
                 n_records=n, note=f"{files} list file(s) from browser harvest")
    return n


# --- Wall Street Journal --------------------------------------------------------

# Subscriber harvest, same shape as NYT: harvest/wsj/<slug>.json holding
#   {list, year, url, books: [{title, author, publisher}]}
# WSJ's year-end article is regular — a title line followed by
# "By <Author> | <Publisher>" — so the in-page extractor is a two-line rule.
WSJ_DIR = os.path.join(HARVEST_DIR, "wsj")
_WSJ_LABELS = {"wsj-10-best": "The 10 Best Books"}


def load_wsj(conn) -> int:
    if not os.path.isdir(WSJ_DIR):
        db.log_fetch(conn, "wsj", False,
                     note=f"{WSJ_DIR}/ missing — Chrome pass needed")
        raise FileNotFoundError(f"{WSJ_DIR}/<slug>.json not found. "
                                "See docs/harvesting.md.")
    n = files = 0
    for fn in sorted(os.listdir(WSJ_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(WSJ_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        slug = payload.get("list") or fn[:-5]
        label = _WSJ_LABELS.get(slug, slug)
        year = payload.get("year")
        year = int(year) if str(year).isdigit() else None
        files += 1
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            # 'Anjet Daanje, translated by David McKay' -> the author
            author = clean_nyt_author(b.get("author"))
            key = db.upsert_work(conn, title, author or None)
            if db.add_accolade(conn, key, "wsj", "list", "listed",
                               category=label, year=year,
                               detail=(b.get("publisher") or None),
                               url=payload.get("url")):
                n += 1
        conn.commit()
    db.log_fetch(conn, "wsj", files > 0, url="https://www.wsj.com/arts-culture/books",
                 n_records=n, note=f"{files} list file(s) from browser harvest")
    return n


# --- FT Business Book of the Year -----------------------------------------------

# ⚠ THE ONE WIKIPEDIA-SOURCED SOURCE, and deliberately so. The award's own page
# (ft.com/bookaward) is an index of articles whose bodies sit behind the FT
# paywall, and the shortlists live in those bodies. With no FT subscription
# there is no original route, so this falls back — and every record it writes
# is stamped 'via Wikipedia' in `detail` so the provenance is never ambiguous.
#
# Structure: '=== <year> ===' sections of bullets, winner flagged {{Blue ribbon}}:
#   * {{Blue ribbon}} [[Parmy Olson]], ''[[Supremacy (book)|Supremacy: AI…]]''
FT_WIKI_PAGE = "Financial Times Business Book of the Year Award"
FT_WIKI_API = ("https://en.wikipedia.org/w/api.php?action=parse&page={}"
               "&prop=wikitext&format=json")
# The lookahead must stop at ANY heading level. Stopping only at '\n===' let
# the final year section run on through the level-2 headings after it, which
# pulled a bullet out of a later section and filed it as a 2025 shortlistee.
_FT_YEAR_RE = re.compile(r"===+\s*(\d{4})\s*===+(.*?)(?=\n==|\Z)", re.S)
_FT_ITALIC_RE = re.compile(r"''+(.+?)''+", re.S)


def _wiki_plain(s: str) -> str:
    """[[Target|Display]] / [[Name]] -> the displayed text; drop templates."""
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s or "")
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", " ", s, flags=re.S)
    s = re.sub(r"'{2,}", "", s)
    return re.sub(r"\s+", " ", s).strip(" ,")


def parse_ft_wikitext(wikitext: str) -> list[dict]:
    out = []
    for year, body in _FT_YEAR_RE.findall(wikitext):
        for line in body.split("\n"):
            line = line.strip()
            # Most years are bullet lists; 2020 is a wiki table, so its entries
            # start with '|' instead of '*' and the whole year was being
            # skipped. An italic title is still required below, which keeps
            # table headers and formatting rows out.
            if not line.startswith(("*", "|")):
                continue
            item = line.lstrip("*|").strip()
            # {{Blue ribbon}} in some years, {{blue ribbon}} in others — a
            # case-sensitive check silently lost 14 of 21 winners.
            status = ("winner" if re.search(r"\{\{\s*blue ribbon", item, re.I)
                      else "shortlist")
            tm = _FT_ITALIC_RE.search(item)
            if not tm:
                continue
            title = _wiki_plain(tm.group(1))
            author = _wiki_plain(item[:tm.start()])
            if not title:
                continue
            out.append({"year": int(year), "title": title,
                        "author": author or None, "status": status})
    return out


def load_ft_business(conn) -> int:
    fails: list = []
    url = FT_WIKI_API.format(FT_WIKI_PAGE.replace(" ", "%20"))
    raw = _fetch(conn, url, fails, accept="application/json", timeout=45)
    if raw is None:
        _warn_if_mostly_failing("ft-business", 0, fails)
        db.log_fetch(conn, "ft-business", False, url=url,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    wikitext = json.loads(raw)["parse"]["wikitext"]["*"]
    entries = parse_ft_wikitext(wikitext)
    n = 0
    for e in entries:
        key = db.upsert_work(conn, e["title"], e["author"])
        conn.execute("UPDATE works SET form = COALESCE(form, 'nonfiction') "
                     "WHERE work_key = ?", (key,))
        if db.add_accolade(conn, key, "ft-business", "award", e["status"],
                           category="Business Book of the Year",
                           year=e["year"], detail="via Wikipedia (FT paywalled)",
                           url="https://www.ft.com/bookaward"):
            n += 1
    conn.commit()
    db.log_fetch(conn, "ft-business", bool(entries), url=url, n_records=n,
                 note=_fetch_note(len(entries), fails,
                                  "entries — WIKIPEDIA FALLBACK, FT paywalled"))
    return n


# --- LA Times Book Prizes -------------------------------------------------------

# latimes.com/events/festival-of-books/book-prizes. Fifteen categories, each an
# <h2 data-element="element-header-title">, with entries as rich-text modules
# holding three short lines: title, author, publisher. The winner's module
# carries `bp-winner-ribbon` in its class list.
#
# The page shows the current cycle; a "History" section links prior years.
LATIMES_URL = ("https://www.latimes.com/events/festival-of-books/book-prizes")
_LAT_H2_RE = re.compile(
    r'<h2[^>]*data-element="element-header-title"[^>]*>(.*?)</h2>', re.S)
_LAT_MOD_SPLIT = '<div data-element="rich-text-module"'
_LAT_FORMS = {"fiction": "novel", "first fiction": "novel",
              "biography": "nonfiction", "history": "nonfiction",
              "current interest": "nonfiction", "science": "nonfiction",
              "technology": "nonfiction", "poetry": "poetry",
              "graphic novel": "collection", "comics": "collection",
              "mystery": "novel", "thriller": "novel",
              "sci-fi": "novel", "fantasy": "novel",
              "young adult": "novel", "audiobook": "audio"}


def _lat_lines(module_html: str) -> list[str]:
    """Visible text of one module, minus images and their srcset noise."""
    body = module_html.split(">", 1)[1] if ">" in module_html else module_html
    body = re.sub(r"<(script|style|figure|picture)\b.*?</\1>", " ", body,
                  flags=re.S | re.I)
    body = re.sub(r"<img[^>]*>", " ", body)
    out = []
    for piece in re.split(r"<[^>]+>", body):
        piece = _strip(piece)
        if piece and "srcset" not in piece and len(piece) < 200:
            out.append(piece)
    return out


def parse_latimes(page: str) -> list[dict]:
    """-> [{category, title, author, publisher, status}] for the shown year."""
    parts = _LAT_H2_RE.split(page)
    out = []
    # parts alternates: [pre, heading, body, heading, body, ...]
    for idx in range(1, len(parts) - 1, 2):
        category = _strip(htmlunescape(parts[idx]))
        section = parts[idx + 1]
        if not category:
            continue
        # The winner's ribbon is its OWN image-only module sitting just before
        # the winner's text module, so the flag has to carry forward rather
        # than be read off the entry itself.
        pending_winner = False
        for mod in section.split(_LAT_MOD_SPLIT)[1:]:
            if "bp-winner-ribbon" in mod[:400]:
                pending_winner = True
            lines = _lat_lines(mod[:4000])
            # an entry is title / author / (publisher); anything else on the
            # page is prose, a sponsor logo or a section lead
            if len(lines) < 2 or len(lines[0]) > 140:
                continue
            if any(len(x) > 160 for x in lines[:2]):
                continue
            title = htmlunescape(lines[0])
            # 'Judges:' introduces the panel, not a book
            if title.rstrip().endswith(":") or re.match(r"judges\b", title, re.I):
                continue
            out.append({
                "category": category,
                "title": title,
                "author": htmlunescape(lines[1]),
                "publisher": htmlunescape(lines[2]) if len(lines) > 2 else None,
                "status": "winner" if pending_winner else "finalist",
            })
            pending_winner = False
    return out


def _lat_form(category: str) -> str:
    c = re.sub(r"\s+", " ", (category or "")).strip().lower()
    for key, form in _LAT_FORMS.items():
        if key in c:
            return form
    return "novel"


def load_latimes(conn) -> int:
    fails: list = []
    raw = _fetch(conn, LATIMES_URL, fails, timeout=60)
    if raw is None:
        _warn_if_mostly_failing("latimes", 0, fails)
        db.log_fetch(conn, "latimes", False, url=LATIMES_URL,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    page = raw.decode("utf-8", "replace")
    ym = re.search(r"(\d{4})\s+Winners", page)
    year = int(ym.group(1)) if ym else None
    entries = parse_latimes(page)
    n = 0
    for e in entries:
        key = db.upsert_work(conn, e["title"], e["author"])
        conn.execute("UPDATE works SET form = COALESCE(form, ?) "
                     "WHERE work_key = ?", (_lat_form(e["category"]), key))
        if db.add_accolade(conn, key, "latimes", "award", e["status"],
                           category=e["category"], year=year,
                           detail=e["publisher"], url=LATIMES_URL):
            n += 1
    conn.commit()
    db.log_fetch(conn, "latimes", bool(entries), url=LATIMES_URL, n_records=n,
                 note=_fetch_note(len(entries), fails,
                                  f"entries for {year} (current cycle only)"))
    return n


# --- Douban annual book lists (豆瓣年度读书榜单) ----------------------------------

# Douban is a *list*, not a juried award: an editorial year-end selection with
# a community rating attached, so it lands as source_kind='list' with the
# rating in `detail` rather than as winner/nominee.
#
# Worth having despite the extra effort — SCCL and San Jose both hold real
# Chinese-language collections, and `bayarea_lookup` already romanizes CJK to
# pinyin for exactly these searches.
#
# Harvest is manual: book.douban.com serves scripted clients a 2.4KB stub, and
# from inside the page Chrome blocks the automatic download while Private
# Network Access blocks the localhost POST. See docs/harvesting.md — it needs
# one click to allow the download. Files land in harvest/douban/<year>.json.
DOUBAN_DIR = os.path.join(HARVEST_DIR, "douban")
# '[波] 雷沙德·卡普希钦斯基' — a bracketed nationality marker precedes translated
# authors and is not part of the name.
_DOUBAN_NATIONALITY_RE = re.compile(r"^\s*[\[［][^\]］]{1,12}[\]］]\s*")


def clean_douban_author(name: str) -> str:
    return _DOUBAN_NATIONALITY_RE.sub("", (name or "")).strip()


def load_douban(conn) -> int:
    if not os.path.isdir(DOUBAN_DIR):
        db.log_fetch(conn, "douban", False,
                     note=f"{DOUBAN_DIR}/ missing — manual Chrome harvest needed")
        raise FileNotFoundError(
            f"{DOUBAN_DIR}/<year>.json not found. See docs/harvesting.md.")
    n = files = 0
    for fn in sorted(os.listdir(DOUBAN_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(DOUBAN_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        year = payload.get("year")
        year = int(year) if str(year).isdigit() else None
        files += 1
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            author = clean_douban_author(demojibake(b.get("author")))
            key = db.upsert_work(conn, title, author or None)
            rating = b.get("rating")
            if db.add_accolade(conn, key, "douban", "list", "listed",
                               category="豆瓣年度读书榜单", year=year,
                               detail=(f"douban {rating}" if rating else None),
                               url=f"https://book.douban.com/annual/{year}/"):
                n += 1
        conn.commit()
    db.log_fetch(conn, "douban", files > 0,
                 url="https://book.douban.com/annual/", n_records=n,
                 note=f"{files} year file(s) from manual harvest")
    return n


# --- PEN America ----------------------------------------------------------------

# pen.org 403s scripted clients like pulitzer.org does, so the winners archive
# is harvested in a Chrome tab (see docs/harvesting.md). Its table paginates
# entirely client-side — no request fires on a page change — so the harvest
# walks all 50 pages in-page and dedupes.
#
# PEN is the source that makes the author/work split concrete: 35 of its 999
# rows carry Title "N/A" because the prize is for a career (PEN/Nabokov,
# PEN/Manheim for translation), not a book. Those go to author_accolades.
_PEN_GENRE_FORMS = {"fiction": "novel", "nonfiction": "nonfiction",
                    "biography": "nonfiction", "essay": "nonfiction",
                    "poetry": "poetry", "drama": "drama",
                    "translation": None, "multi-genre": None,
                    "children": "novel", "science writing": "nonfiction"}


def _pen_form(genre: str) -> str | None:
    g = re.sub(r"\s+", " ", (genre or "")).strip().lower()
    for key, form in _PEN_GENRE_FORMS.items():
        if key in g:
            return form
    return None


def load_pen(conn) -> int:
    path = os.path.join(HARVEST_DIR, "pen.json")
    if not os.path.exists(path):
        db.log_fetch(conn, "pen", False,
                     note=f"{path} missing — Chrome pass needed")
        raise FileNotFoundError(
            f"{path} not found. Run the Chrome harvest first: "
            "uv run acclaim.py browser-plan")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)

    n = career = 0
    for r in rows:
        year = r.get("year", "")
        year = int(year) if str(year).isdigit() else None
        who = demojibake(" ".join(x for x in (r.get("first"), r.get("last"))
                                  if x and x != "N/A").strip())
        title = demojibake((r.get("title") or "").strip())
        award = (r.get("award") or "").strip() or "PEN America"
        if not title or title == "N/A":
            # a career award: no book to attach it to
            if who and db.add_author_accolade(
                    conn, who, "pen", "winner", year=year, detail=award,
                    url="https://pen.org/literary-awards/"
                        "literary-awards-winners-archive/"):
                career += 1
            continue
        key = db.upsert_work(conn, title, who or None)
        form = _pen_form(r.get("genre"))
        if form:
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (form, key))
        if db.add_accolade(conn, key, "pen", "award", "winner",
                           category=award, year=year,
                           detail=(r.get("genre") or None),
                           url="https://pen.org/literary-awards/"
                               "literary-awards-winners-archive/"):
            n += 1
    conn.commit()
    db.log_fetch(conn, "pen", True,
                 url="https://pen.org/literary-awards/"
                     "literary-awards-winners-archive/",
                 n_records=n,
                 note=f"{len(rows)} archive rows; {career} career-level "
                      f"to author_accolades; archive ends 2023")
    return n


# --- Goodreads Choice Awards ----------------------------------------------------

# The only popular-vote award here, and worth keeping separate in the head from
# the juried ones: it measures what a large self-selected readership liked, not
# what a jury judged. Structure per year:
#   /choiceawards/best-books-<year>   -> category links + names
#   /choiceawards/<slug>-<year>       -> winner + 20 nominees
# Nominee credits live in the cover image's alt text as "Title by Author", and
# the winner additionally carries <a class="winningTitle ...">.
GOODREADS_YEAR = "https://www.goodreads.com/choiceawards/best-books-{}"
GOODREADS_FIRST_YEAR = 2009
_GR_CAT_RE = re.compile(
    r'href="(/choiceawards/[a-z0-9-]+-(\d{4}))"[^>]*>\s*'
    r"<h4 class='category__copy'>\s*(.*?)\s*</h4>", re.S)
_GR_NOMINEE_RE = re.compile(
    r'pollAnswer__bookLink"[^>]*>\s*<img alt="([^"]{2,160})"')
_GR_WINNER_RE = re.compile(r'class="winningTitle[^"]*"[^>]*>(.*?)</a>', re.S)
# Goodreads categories are all books, but they span forms.
_GR_FORMS = {"nonfiction": "nonfiction", "memoir": "nonfiction",
             "autobiography": "nonfiction", "history": "nonfiction",
             "biography": "nonfiction", "poetry": "poetry",
             "graphic novels": "collection", "comics": "collection",
             "short stories": "collection", "food": "nonfiction",
             "cookbooks": "nonfiction", "science": "nonfiction",
             "business": "nonfiction", "travel": "nonfiction"}


def parse_goodreads_category(page: str) -> list[dict]:
    """-> [{title, author, status}] for one Choice Awards category page."""
    win = _GR_WINNER_RE.search(page)
    winner_title = _flat_name(_strip(win.group(1))) if win else None
    out = []
    for alt in _GR_NOMINEE_RE.findall(page):
        alt = re.sub(r"\s+", " ", alt).strip()
        # 'Title by Author'; titles can contain ' by ', so split on the last one
        parts = re.split(r"\s+by\s+", alt)
        if len(parts) < 2:
            title, author = alt, None
        else:
            title, author = " by ".join(parts[:-1]).strip(), parts[-1].strip()
        if not title:
            continue
        status = "winner" if (winner_title
                              and _flat_name(title) == winner_title) else "nominee"
        out.append({"title": title, "author": author, "status": status})
    return out


def _gr_form(category: str) -> str:
    c = re.sub(r"\s+", " ", (category or "")).strip().lower()
    for key, form in _GR_FORMS.items():
        if key in c:
            return form
    return "novel"


def load_goodreads(conn) -> int:
    import datetime
    fails: list = []
    n = years_ok = 0
    for year in range(GOODREADS_FIRST_YEAR, datetime.date.today().year + 1):
        raw = _fetch(conn, GOODREADS_YEAR.format(year), fails, timeout=60)
        if raw is None:
            continue
        landing = raw.decode("utf-8", "replace")
        cats = {(href, _strip(name)) for href, yr, name in
                _GR_CAT_RE.findall(landing) if yr == str(year)}
        got = False
        for href, name in sorted(cats):
            curl = "https://www.goodreads.com" + href
            craw = _fetch(conn, curl, fails, timeout=60)
            if craw is None:
                continue
            form = _gr_form(name)
            for e in parse_goodreads_category(craw.decode("utf-8", "replace")):
                got = True
                key = db.upsert_work(conn, e["title"], e["author"])
                conn.execute(
                    "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                    (form, key))
                if db.add_accolade(conn, key, "goodreads", "award", e["status"],
                                   category=name, year=year, url=curl):
                    n += 1
            conn.commit()          # before the next fetch — see load_booker
        years_ok += bool(got)
    _warn_if_mostly_failing("goodreads", years_ok, fails)
    db.log_fetch(conn, "goodreads", years_ok > 0,
                 url=GOODREADS_YEAR.format("<year>"), n_records=n,
                 note=_fetch_note(years_ok, fails, "years (popular vote)"))
    return n


# --- National Book Critics Circle -----------------------------------------------

# bookcritics.org lays each award out as
#   <h3>Category</h3><h4>Author</h4><p class="book-title">Title</p>
# inside an #award-winners / #award-finalists section. The h4 often carries
# more than a name — "Han Kang, translated from the Korean by e. yaewon and
# Paige Aniyah Morris" — so the author is the part before the translator note.
#
# Coverage caveat: the landing page carries only the current cycle. Historical
# years live on separate pages that are not yet wired up, so this source starts
# shallow by design and `stats` will show it.
NBCC_URL = "https://www.bookcritics.org/awards/"
_NBCC_ITEM_RE = re.compile(
    r"<h3[^>]*>(.*?)</h3>\s*<h4[^>]*>(.*?)</h4>\s*"
    r'<p class="book-title"[^>]*>(.*?)</p>', re.S | re.I)
_NBCC_YEAR_RE = re.compile(r"(\d{4})\s+NBCC", re.I)
_NBCC_TRANSLATOR_RE = re.compile(
    r",?\s*(translated|edited|with)\b.*$", re.I)
_NBCC_FORMS = {"fiction": "novel", "nonfiction": "nonfiction",
               "biography": "nonfiction", "autobiography": "nonfiction",
               "criticism": "nonfiction", "poetry": "poetry"}


def parse_nbcc(page: str) -> list[dict]:
    """-> [{category, author, title, status, year}]."""
    out = []
    for section, status in (("award-winners", "winner"),
                            ("award-finalists", "finalist")):
        i = page.find(f'id="{section}"')
        if i == -1:
            continue
        nxt = page.find('id="award-', i + 10)
        chunk = page[i:nxt if nxt != -1 else len(page)]
        ym = _NBCC_YEAR_RE.search(_strip(chunk[:400]))
        year = int(ym.group(1)) if ym else None
        for cat, author, title in _NBCC_ITEM_RE.findall(chunk):
            author = _NBCC_TRANSLATOR_RE.sub("", _strip(author)).strip(" ,")
            title, cat = _strip(title), _strip(cat)
            if title and cat:
                out.append({"category": cat, "author": author or None,
                            "title": title, "status": status, "year": year})
    return out


def load_nbcc(conn) -> int:
    fails: list = []
    raw = _fetch(conn, NBCC_URL, fails, timeout=60)
    if raw is None:
        _warn_if_mostly_failing("nbcc", 0, fails)
        db.log_fetch(conn, "nbcc", False, url=NBCC_URL,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    entries = parse_nbcc(raw.decode("utf-8", "replace"))
    n = 0
    for e in entries:
        key = db.upsert_work(conn, e["title"], e["author"])
        form = _NBCC_FORMS.get(e["category"].split()[0].lower())
        if form:
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (form, key))
        if db.add_accolade(conn, key, "nbcc", "award", e["status"],
                           category=e["category"], year=e["year"],
                           url=NBCC_URL):
            n += 1
    conn.commit()
    db.log_fetch(conn, "nbcc", bool(entries), url=NBCC_URL, n_records=n,
                 note=_fetch_note(len(entries), fails,
                                  "entries (current cycle only)"))
    return n


# --- Nobel Prize in Literature --------------------------------------------------

# The Nobel has a real JSON API, which makes it the cheapest source here — and
# the only one so far that is awarded to a *person* for a body of work rather
# than to a book. It therefore writes to `author_accolades`, not `accolades`:
# inventing a work called "Han Kang" would both fabricate a book and inflate
# every work-level score that counts distinct awards.
NOBEL_API = ("https://api.nobelprize.org/2.1/nobelPrizes"
             "?nobelPrizeCategory=lit&limit=200&sort=asc")


def load_nobel(conn) -> int:
    fails: list = []
    raw = _fetch(conn, NOBEL_API, fails, accept="application/json", timeout=45)
    if raw is None:
        _warn_if_mostly_failing("nobel", 0, fails)
        db.log_fetch(conn, "nobel", False, url=NOBEL_API,
                     note=_fetch_note(0, fails, "prizes"))
        return 0
    payload = json.loads(raw)
    n = 0
    for prize in payload.get("nobelPrizes", []):
        year = prize.get("awardYear")
        year = int(year) if str(year).isdigit() else None
        for laureate in prize.get("laureates", []):
            name = (laureate.get("knownName") or {}).get("en") \
                or (laureate.get("fullName") or {}).get("en")
            if not name:
                continue                       # years the prize was not awarded
            motivation = (laureate.get("motivation") or {}).get("en")
            if db.add_author_accolade(conn, name, "nobel", "winner", year=year,
                                      detail=motivation,
                                      url="https://www.nobelprize.org/prizes/literature/"):
                n += 1
    conn.commit()
    db.log_fetch(conn, "nobel", True, url=NOBEL_API, n_records=n,
                 note=_fetch_note(len(payload.get("nobelPrizes", [])), fails,
                                  "prize years (author-level)"))
    return n


def _load_obama_harvest(conn, path: str) -> int:
    """harvest/obama.json: [{year, url, books:[{title, author}]}]."""
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    n = posts = 0
    for post in payload:
        year = post.get("year")
        year = int(year) if str(year).isdigit() else None
        posts += 1
        for b in post.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            author = demojibake((b.get("author") or "").strip()) or None
            key = db.upsert_work(conn, title, author)
            if db.add_accolade(conn, key, "obama", "list", "listed",
                               category="Obama's favorites", year=year,
                               url=post.get("url")):
                n += 1
        conn.commit()
    db.log_fetch(conn, "obama", posts > 0, url=OBAMA_FEED, n_records=n,
                 note=f"{posts} post(s) from browser harvest")
    return n


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
        Source("nyt", "New York Times book lists", "list", BROWSER, load_nyt,
               cadence="annual-nov",
               note="subscriber harvest; 100 Best of the 21st C. loaded",
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
               note="15 categories; page shows the current cycle only",
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
               note="Medium is the scriptable original; obama.org renders via JS",
               forms=("novel", "nonfiction")),
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
    ]
}


# --- cli ------------------------------------------------------------------------

def cmd_pull(args) -> int:
    conn = db.open_db(args.db)
    keys = ([args.source] if args.source
            else [k for k, s in SOURCES.items()
                  if args.include_browser or s.transport != BROWSER])
    total = 0
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
        except FileNotFoundError as exc:
            print(f"{src.key:<14} SKIPPED — {exc}", file=sys.stderr)
        except Exception as exc:                        # noqa: BLE001
            db.log_fetch(conn, src.key, False, note=f"{type(exc).__name__}: {exc}")
            print(f"{src.key:<14} FAILED — {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    print(f"total +{total}")
    return 0


def cmd_browser_plan(args) -> int:
    """What still needs a human-driven Chrome pass, and why."""
    print("These sources cannot run unattended:\n")
    for s in SOURCES.values():
        if s.transport != BROWSER:
            continue
        have = os.path.exists(os.path.join(HARVEST_DIR, f"{s.key}.json"))
        print(f"  {s.key:<14} {'harvested' if have else 'NOT HARVESTED':<14} {s.note}")
    print(f"\nHarvest files land in {HARVEST_DIR}/ via tools/collector.py.")
    print("See docs/harvesting.md for the per-source Chrome recipe.")
    return 0


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
        if author and _flat_name(author) not in _flat_name(page[:4000]):
            continue                        # a different work of the same name
        for pid, name in _ISFDB_PUB_RE.findall(page):
            name = _strip(name)
            if not name or pid in seen:
                continue
            seen.add(pid)
            out.append({"container": name, "isfdb_pub": pid,
                        "standalone": _flat_name(name) == _flat_name(title)})
    return out


def _flat_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


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
        for sysname in ("sccl", "sjpl"):
            for cand in hotlist.bc_bibs(
                    sysname, {"title": name, "author": author or "",
                              "isbns": [], "formats": ("book",)}):
                if cand["format_class"] != "book":
                    continue
                print(f"      {sysname:<5} {cand['available'] or 0} on shelf / "
                      f"{cand['copies'] or 0} copies   {cand['url']}")
    return 0


# --- scoring --------------------------------------------------------------------

# Counting *distinct sources*, not rows, is the whole point. Locus alone
# carries 5,214 accolades because its nominee lists run ten deep in every
# category; ranking on raw accolade count would put a mid-list Locus nominee
# above a Pulitzer winner. A book that shows up across many independent
# juries is the signal — the same work winning one prize twice is not.
_WON = ("winner",)
_NOMINATED = ("finalist", "shortlist", "longlist", "nominee")
SCORE_WEIGHTS = {"won": 3.0, "nominated": 1.0, "listed": 2.0}

# …but "distinct sources" is not the same as "independent juries", and the
# first run of this scorer proved it: the entire top of the table was science
# fiction, because Hugo, Nebula and Locus are three near-parallel juries
# voting on substantially the same ballot. An SF novel banked three wins where
# a Pulitzer winner banked one — so the metric was rewarding *redundant*
# juries, not breadth. Sources that share a constituency collapse to one
# family before anything is counted.
AWARD_FAMILIES = {
    "hugo": "sf", "nebula": "sf", "locus": "sf",
    "booker": "booker", "booker-intl": "booker", "booker-childrens": "booker",
}


def award_family(source: str) -> str:
    return AWARD_FAMILIES.get(source, source)


def compute_scores(conn) -> int:
    """Recompute `work_scores` from scratch. Never incremental: a rerun after
    a parser fix must produce the same numbers, not accumulate on top."""
    conn.execute("DELETE FROM work_scores")
    # Aggregated in Python rather than SQL so the family collapse is visible
    # and testable; the table is small enough that it costs nothing.
    per_work: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT work_key, source, source_kind, status FROM accolades"):
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

def cmd_shelf(args) -> int:
    """Acclaimed *and* borrowable: the point of keeping this next to the catalog.

    Live lookups, so it is deliberately capped — this walks the top N scored
    works through the same BiblioCommons search the want-list uses.
    """
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
        entry = {"title": r["title"], "author": r["author"] or "",
                 "isbns": [], "formats": ("book",)}
        hits = []
        for sysname in args.system:
            for c in hotlist.bc_bibs(sysname, entry):
                if c["format_class"] != "book":
                    continue
                hits.append((sysname, c))
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

    sh = sub.add_parser("shelf", help="acclaimed AND borrowable near you now")
    sh.add_argument("--limit", type=int, default=40,
                    help="how many top-scored works to check live (default 40)")
    sh.add_argument("--min-score", type=float, default=4.0)
    sh.add_argument("--system", action="append",
                    choices=["sccl", "sjpl"], default=None)
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
            "stats": cmd_stats, "score": cmd_score,
            "shelf": cmd_shelf, "find": cmd_find}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
