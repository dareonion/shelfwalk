"""Booker — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
                 n_records=n, n_parsed=pages,
                 note=_fetch_note(pages, fails, f"book pages of {len(books)}"))
    return n
