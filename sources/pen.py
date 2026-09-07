"""PEN America — see acclaim_core for the shared machinery."""
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

# --- PEN America ----------------------------------------------------------------

# pen.org 403s scripted clients like pulitzer.org does, so the winners archive
# is harvested in a Chrome tab (see docs/harvesting.md). Its table paginates
# entirely client-side — no request fires on a page change — so the harvest
# walks all 50 pages in-page and dedupes.
#
# PEN is the source that makes the author/work split concrete: 35 of its 999
# rows carry Title "N/A" because the prize is for a career (PEN/Nabokov,
# PEN/Manheim for translation), not a book. Those go to author_accolades.
_PEN_GENRE_FORMS = {"fiction": "novel", "nonfiction": "nonfiction",
                    "biography": "nonfiction", "essay": "nonfiction",
                    "poetry": "poetry", "drama": "drama",
                    "translation": None, "multi-genre": None,
                    "children": "novel", "science writing": "nonfiction"}


def _pen_form(genre: str) -> str | None:
    g = re.sub(r"\s+", " ", (genre or "")).strip().lower()
    for key, form in _PEN_GENRE_FORMS.items():
        if key in g:
            return form
    return None


def load_pen(conn) -> int:
    path = os.path.join(HARVEST_DIR, "pen.json")
    if not os.path.exists(path):
        db.log_fetch(conn, "pen", False,
                     note=f"{path} missing — Chrome pass needed")
        raise FileNotFoundError(
            f"{path} not found. Run the Chrome harvest first: "
            "uv run acclaim.py browser-plan")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)

    n = career = 0
    for r in rows:
        year = r.get("year", "")
        year = int(year) if str(year).isdigit() else None
        who = demojibake(" ".join(x for x in (r.get("first"), r.get("last"))
                                  if x and x != "N/A").strip())
        title = demojibake((r.get("title") or "").strip())
        award = (r.get("award") or "").strip() or "PEN America"
        if not title or title == "N/A":
            # a career award: no book to attach it to
            if who and db.add_author_accolade(
                    conn, who, "pen", "winner", year=year, detail=award,
                    url="https://pen.org/literary-awards/"
                        "literary-awards-winners-archive/"):
                career += 1
            continue
        key = db.upsert_work(conn, title, who or None)
        form = _pen_form(r.get("genre"))
        if form:
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (form, key))
        if db.add_accolade(conn, key, "pen", "award", "winner",
                           category=award, year=year,
                           detail=(r.get("genre") or None),
                           url="https://pen.org/literary-awards/"
                               "literary-awards-winners-archive/"):
            n += 1
    conn.commit()
    db.log_fetch(conn, "pen", True,
                 url="https://pen.org/literary-awards/"
                     "literary-awards-winners-archive/",
                 n_records=n,
                 note=f"{len(rows)} archive rows; {career} career-level "
                      f"to author_accolades; archive ends 2023")
    return n
