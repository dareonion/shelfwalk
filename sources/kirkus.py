"""Kirkus Prize — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Kirkus Prize ---------------------------------------------------------------

# kirkusreviews.com/prize/<year>/ — one page per year since 2014, and the two
# halves are marked up differently:
#   winners   <div class="prize-winner">  <p class="prize-label">FICTION</p>
#                                         <h2><a>JAMES</a></h2>
#                                         <p class="prize-label">BY …</p>
#   finalists <section class="prize-finalists"> <h2 class="prize-category">…</h2>
#                                         <li><p class="book-title"><a>…</a></p>
#                                             <p>By …</p></li>
KIRKUS_YEAR = "https://www.kirkusreviews.com/prize/{}/"
KIRKUS_FIRST_YEAR = 2014
_KP_WINNER_RE = re.compile(
    r'<div class="prize-winner[^"]*">(.*?)</div>', re.S | re.I)
_KP_LABEL_RE = re.compile(r'<p class="prize-label">(.*?)</p>', re.S | re.I)
_KP_H2A_RE = re.compile(r"<h2>\s*<a[^>]*>(.*?)</a>\s*</h2>", re.S | re.I)
_KP_CATEGORY_RE = re.compile(r'<h2 class="prize-category">(.*?)</h2>', re.S | re.I)
_KP_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_KP_TITLE_RE = re.compile(
    r'<p class="book-title">\s*<a[^>]*>(.*?)</a>\s*</p>', re.S | re.I)
_KP_BY_RE = re.compile(r"<p>\s*By\s+(.*?)</p>", re.S | re.I)
_KIRKUS_FORMS = {"fiction": "novel", "nonfiction": "nonfiction",
                 "non-fiction": "nonfiction", "young readers": "novel",
                 "young readers' literature": "novel"}





def parse_kirkus_year(page: str) -> list[dict]:
    out = []
    for block in _KP_WINNER_RE.findall(page):
        labels = _KP_LABEL_RE.findall(block)
        tm = _KP_H2A_RE.search(block)
        if not tm or not labels:
            continue
        author = next((_strip(x) for x in labels[1:]
                       if re.match(r"\s*by\s+", _strip(x), re.I)), "")
        out.append({"category": _kp_title_case(labels[0]),
                    "title": _kp_title_case(tm.group(1)),
                    "author": _kp_title_case(re.sub(r"^\s*by\s+", "", author,
                                                    flags=re.I)) or None,
                    "status": "winner"})

    i = page.find('class="reviews-section prize-finalists"')
    if i != -1:
        section = page[i:]
        parts = _KP_CATEGORY_RE.split(section)
        for k in range(1, len(parts) - 1, 2):
            category = _kp_title_case(parts[k])
            for li in _KP_LI_RE.findall(parts[k + 1]):
                tm = _KP_TITLE_RE.search(li)
                if not tm:
                    continue
                bm = _KP_BY_RE.search(li)
                out.append({"category": category,
                            "title": _kp_title_case(tm.group(1)),
                            "author": _kp_title_case(bm.group(1)) if bm else None,
                            "status": "finalist"})
    return out


def _kirkus_form(category: str) -> str:
    c = re.sub(r"\s+", " ", (category or "")).strip().lower()
    for key, form in _KIRKUS_FORMS.items():
        if key in c:
            return form
    return "novel"


def load_kirkus(conn) -> int:
    import datetime
    fails: list = []
    n = years_ok = parsed = 0
    for year in range(KIRKUS_FIRST_YEAR.date.today().year + 1):
        raw = _fetch(conn, KIRKUS_YEAR.format(year), fails, timeout=60)
        if raw is None:
            continue
        entries = parse_kirkus_year(raw.decode("utf-8", "replace"))
        parsed += len(entries)
        if entries:
            years_ok += 1
        for e in entries:
            if not e["title"]:
                continue
            key = db.upsert_work(conn, e["title"], e["author"])
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (_kirkus_form(e["category"]), key))
            if db.add_accolade(conn, key, "kirkus", "award", e["status"],
                               category=e["category"], year=year,
                               url=KIRKUS_YEAR.format(year)):
                n += 1
        conn.commit()
    _warn_if_mostly_failing("kirkus", years_ok, fails)
    db.log_fetch(conn, "kirkus", years_ok > 0,
                 url="https://www.kirkusreviews.com/prize/", n_records=n,
                 n_parsed=parsed,
                 note=_fetch_note(years_ok, fails, "years"))
    return n
