"""National Book Awards — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
    n = years_ok = parsed = 0
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
            _nba_entries = parse_nba_page(craw.decode("utf-8", "replace"))
            parsed += len(_nba_entries)
            for e in _nba_entries:
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
                 url="https://www.nationalbook.org/", n_records=n, n_parsed=parsed,
                 note=_fetch_note(years_ok, fails, "years parsed"))
    return n
