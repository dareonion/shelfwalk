"""Nobel Prize in Literature — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

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
                 n_parsed=len(payload.get("nobelPrizes", [])),
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
