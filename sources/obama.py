"""Obama's reading lists — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
# Where the books stop: any short heading that names music or film — the
# year-end post's "Favorite Movies of <year>", the summer post's "Summer
# Playlist:" — ends the book section.
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


def _load_obama_harvest(conn, path: str) -> int:
    """harvest/obama.json: [{year, url, books:[{title, author}]}]."""
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    n = posts = parsed = 0
    for post in payload:
        year = post.get("year")
        year = int(year) if str(year).isdigit() else None
        posts += 1
        for b in post.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            parsed += 1
            author = demojibake((b.get("author") or "").strip()) or None
            key = db.upsert_work(conn, title, author)
            if db.add_accolade(conn, key, "obama", "list", "listed",
                               category="Obama's favorites", year=year,
                               url=post.get("url")):
                n += 1
        conn.commit()
    db.log_fetch(conn, "obama", posts > 0, url=OBAMA_FEED, n_records=n,
                 n_parsed=parsed, note=f"{posts} post(s) from browser harvest")
    return n


def load_obama(conn) -> int:
    """Medium 403s scripted clients persistently, so a browser harvest at
    harvest/obama.json wins when present; the HTTP path is kept for when
    Medium allows it."""
    path = os.path.join(HARVEST_DIR, "obama.json")
    if os.path.exists(path):
        return _load_obama_harvest(conn, path)
    import bayarea_lookup as B
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    n = posts = parsed = 0
    fails: list = []
    for url in _obama_post_urls(conn):
        raw = _fetch(conn, url, fails)
        if raw is None:
            continue
        html = raw.decode("utf-8", "replace")
        ym = re.search(r"(?:of|list[s]?)[- ](\d{4})", url) or re.search(r"(\d{4})", url)
        year = int(ym.group(1)) if ym else None
        entries = parse_obama_post(html)
        parsed += len(entries)
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
                 n_parsed=parsed, note=_fetch_note(posts, fails, "posts parsed"))
    return n
