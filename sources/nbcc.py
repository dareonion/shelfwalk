"""National Book Critics Circle — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
                 n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails,
                                  "entries (current cycle only)"))
    return n
