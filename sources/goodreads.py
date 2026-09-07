"""Goodreads Choice Awards — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
    n = years_ok = parsed = 0
    for year in range(GOODREADS_FIRST_YEAR.date.today().year + 1):
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
            _gr = parse_goodreads_category(craw.decode("utf-8", "replace"))
            parsed += len(_gr)
            for e in _gr:
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
                 n_parsed=parsed,
                 note=_fetch_note(years_ok, fails, "years (popular vote)"))
    return n
