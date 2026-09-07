"""Grammy: Best Audio Book / Spoken Word — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Grammy: Best Audio Book / Spoken Word --------------------------------------

# ⚠ Wikipedia fallback, like ft-business. grammy.com refuses scripted clients
# and would need a page per ceremony (60+); the Wikipedia article carries the
# whole category history in one place. Every row is stamped 'via Wikipedia'.
#
# Table shape: Year | Work | Performing Artist, with the winner row carrying a
# yellow background and nominees following it under a rowspan year cell. For an
# audiobook the "performing artist" IS the narrator, so it lands there.
GRAMMY_WIKI_PAGE = ("Grammy Award for Best Audio Book, "
                    "Narration & Storytelling Recording")
_GY_YEAR_RE = re.compile(r"Annual Grammy Awards\|(\d{4})\]\]")
_GY_WINNER_RE = re.compile(r"background:\s*#FAEB86", re.I)


def parse_grammy_wikitext(wikitext: str) -> list[dict]:
    out, year, status = [], None, None
    for row in re.split(r"\n\|-", wikitext):
        ym = _GY_YEAR_RE.search(row)
        if ym:
            year = int(ym.group(1))
        if year is None:
            continue
        # a yellow row opens a new award year's winner; the rows after it are
        # that year's nominees until the next yellow row
        if _GY_WINNER_RE.search(row):
            status = "winner"
        elif ym:
            status = "winner"
        else:
            status = "nominee" if status else None
        cells = [c.strip() for c in re.split(r"\n\|(?!-)", row)[1:]]
        cells = [c for c in cells if c and not c.startswith("!")]
        if len(cells) < 2:
            continue
        title = _wiki_plain(cells[0])
        artist = _wiki_plain(cells[1])
        if not title or len(title) > 160:
            continue
        # the article's infobox is pipe-delimited too and parses as a row;
        # its cells are 'name = ...' / 'awarded_for = ...' parameter syntax
        if "=" in title.split(" ")[0] or re.match(r"^\w+\s*=\s", title):
            continue
        if artist and re.match(r"^\w+\s*=\s", artist):
            continue
        out.append({"year": year, "title": title,
                    "narrator": artist or None,
                    "status": status or "nominee"})
        status = "nominee"
    return out


def load_grammy(conn) -> int:
    fails: list = []
    url = ("https://en.wikipedia.org/w/api.php?action=parse&page="
           + urllib.parse.quote(GRAMMY_WIKI_PAGE)
           + "&prop=wikitext&format=json")
    raw = _fetch(conn, url, fails, accept="application/json", timeout=45)
    if raw is None:
        _warn_if_mostly_failing("grammy", 0, fails)
        db.log_fetch(conn, "grammy", False, url=url,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    entries = parse_grammy_wikitext(json.loads(raw)["parse"]["wikitext"]["*"])
    n = 0
    for e in entries:
        # the performing artist is the best author guess we have; for spoken
        # word they are usually the same person
        key = db.upsert_work(conn, e["title"], e["narrator"])
        conn.execute("UPDATE works SET form = COALESCE(form, 'audio') "
                     "WHERE work_key = ?", (key,))
        if db.add_accolade(conn, key, "grammy", "award", e["status"],
                           category="Best Audio Book / Spoken Word",
                           year=e["year"], narrator=e["narrator"],
                           detail="via Wikipedia (grammy.com blocks scripts)",
                           url="https://www.grammy.com/awards"):
            n += 1
    conn.commit()
    db.log_fetch(conn, "grammy", bool(entries), url=url, n_records=n,
                 n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails,
                                  "entries — WIKIPEDIA FALLBACK"))
    return n
