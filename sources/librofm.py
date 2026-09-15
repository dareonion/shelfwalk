"""Libro.fm bestselling audiobooks — see acclaim_core for the shared machinery."""
from __future__ import annotations

import datetime
import re
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)
from sources.apple_audio import first_author

# --- Libro.fm bestsellers -------------------------------------------------------

# libro.fm/bestsellers is the top 100 across the site (indie-bookstore audiobook
# sales), server-rendered. robots.txt allows it. Per-genre charts exist at
# /bestsellers/<genre> and are not loaded. Each entry is a
# <section class="detailed-list-item"> carrying:
#   <div class="… bestseller-title"><a>Bestseller #36</a></div>
#   <h2 class="margin-bottom0"><a>Taipei Story</a></h2>
#   <p class="lead …">A Novel</p>                      (subtitle, optional)
#   <strong>By:</strong> <a>R. F. Kuang</a> …
#   <strong>Narrated by:</strong> <a>Carolyn Kang</a>, <a>…</a> & <a>…</a>
#   <strong>Length:</strong> 10 hours 25 minutes
# The chart carries no date, so a load records the day it ran.
LIBROFM_URL = "https://libro.fm/bestsellers"
LIBROFM_CHART = "Bestsellers"
_LF_ITEM_SPLIT = '<section class="detailed-list-item">'
_LF_RANK_RE = re.compile(r"Bestseller #(\d+)")
_LF_TITLE_RE = re.compile(r'<h2 class="margin-bottom0">\s*<a[^>]*>(.*?)</a>\s*</h2>', re.S)
_LF_SUBTITLE_RE = re.compile(r'<p class="lead margin-bottom0[^"]*">(.*?)</p>', re.S)
_LF_FIELD_RE = r"<strong>{}:</strong>(.*?)(?:<br|</p>)"
_LF_ANCHOR_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
# Book-club and series tags the store appends to the title proper.
_LF_TAG_RE = re.compile(
    r"\s*:\s*(?:A\s+)?(?:GMA|Reese's|Oprah's)[^:]*?Book Club(?:\s+Pick)?\s*$", re.I)
_LF_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def clean_librofm_title(title: str) -> str:
    """Drop the book-club and series tags the store appends to a title."""
    t = htmlunescape(title or "").strip()
    prev = None
    while t != prev:
        prev = t
        t = _LF_TAG_RE.sub("", _LF_PAREN_RE.sub("", t)).strip()
    return t


def _names(fragment: str) -> list[str]:
    return [_strip(htmlunescape(a)) for a in _LF_ANCHOR_RE.findall(fragment or "")
            if _strip(a)]


def parse_librofm(page: str) -> list[dict]:
    """-> [{rank, title, subtitle, authors, narrators, length}] in chart order."""
    out = []
    for block in page.split(_LF_ITEM_SPLIT)[1:]:
        rm, tm = _LF_RANK_RE.search(block), _LF_TITLE_RE.search(block)
        if not rm or not tm:
            continue
        sm = _LF_SUBTITLE_RE.search(block)
        fields = {}
        for name in ("By", "Narrated by", "Length"):
            m = re.search(_LF_FIELD_RE.format(name), block, re.S)
            fields[name] = m.group(1) if m else ""
        out.append({
            "rank": int(rm.group(1)),
            "title": clean_librofm_title(_strip(tm.group(1))),
            "subtitle": _strip(htmlunescape(sm.group(1))) if sm else None,
            "authors": _names(fields["By"]),
            "narrators": _names(fields["Narrated by"]),
            "length": _strip(fields["Length"]) or None,
        })
    return out


def load_librofm(conn) -> int:
    fails: list = []
    raw = _fetch(conn, LIBROFM_URL, fails, timeout=60, fresh=True)
    entries = parse_librofm(raw.decode("utf-8", "replace")) if raw else []
    today = datetime.date.today()
    n = 0
    for e in entries:
        if not e["title"]:
            continue
        key = db.upsert_work(conn, e["title"],
                             first_author(e["authors"][0]) if e["authors"] else None,
                             subtitle=e["subtitle"])
        if db.add_accolade(conn, key, "librofm", "popularity", "listed",
                           category=LIBROFM_CHART, year=today.year,
                           detail=f"rank {e['rank']} on {today.isoformat()}",
                           narrator=", ".join(e["narrators"]) or None,
                           url=LIBROFM_URL):
            n += 1
    conn.commit()
    db.log_fetch(conn, "librofm", bool(entries), url=LIBROFM_URL, n_records=n,
                 n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails, "chart entries"))
    return n
