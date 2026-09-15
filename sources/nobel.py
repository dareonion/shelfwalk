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

# A real JSON API. The prize goes to a *person* for a body of work, so it writes
# to `author_accolades`, not `accolades`: a work called "Han Kang" would
# fabricate a book and inflate every work-level score.
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
