"""New York Times lists — see acclaim_core for the shared machinery."""
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

# --- New York Times lists -------------------------------------------------------

# Harvested in a Chrome tab against Darren's own subscription. The interactive
# lists render client-side and the site is hostile to scripted clients, so the
# page does the reading and the result lands in harvest/nyt/<slug>.json as
#   {list, year, books: [{rank, title, author, year}]}
#
# Two shapes bit while extracting the 100 Best Books of the 21st Century, and
# both are the same lesson — a fixed line offset is a guess about layout:
#   * six titles wrap onto two lines, putting the byline a line further down;
#   * #88 "The Collected Stories of Lydia Davis" carries its author inside the
#     title, so its byline line is a bare year.
NYT_DIR = os.path.join(HARVEST_DIR, "nyt")
_NYT_TRANSLATOR_RE = re.compile(r"\s*[;,]\s*(translated|edited|with)\b.*$", re.I)
_NYT_LABELS = {
    "nyt-100-21c": "100 Best Books of the 21st Century",
    "nyt-10-best": "The 10 Best Books",
    "nyt-notable": "100 Notable Books",
}


def clean_nyt_author(name: str) -> str:
    """'Elena Ferrante; translated by Ann Goldstein' -> 'Elena Ferrante'."""
    return _NYT_TRANSLATOR_RE.sub("", demojibake(name or "")).strip(" ,;")


def load_nyt(conn) -> int:
    if not os.path.isdir(NYT_DIR):
        db.log_fetch(conn, "nyt", False,
                     note=f"{NYT_DIR}/ missing — Chrome pass needed")
        raise FileNotFoundError(f"{NYT_DIR}/<slug>.json not found. "
                                "See docs/harvesting.md.")
    n = files = parsed = 0
    for fn in sorted(os.listdir(NYT_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(NYT_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        slug = payload.get("list") or fn[:-5]
        label = _NYT_LABELS.get(slug, slug)
        listed_year = payload.get("year")
        listed_year = int(listed_year) if str(listed_year).isdigit() else None
        files += 1
        parsed += len(payload.get("books", []))
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            key = db.upsert_work(conn, title, clean_nyt_author(b.get("author")) or None,
                                 pub_year=str(b["year"]) if b.get("year") else None)
            rank = b.get("rank")
            if db.add_accolade(conn, key, "nyt", "list", "listed",
                               category=label, year=listed_year,
                               detail=(f"rank {rank}" if rank else None),
                               url="https://www.nytimes.com/books/"):
                n += 1
        conn.commit()
    db.log_fetch(conn, "nyt", files > 0, url="https://www.nytimes.com/books/",
                 n_records=n, n_parsed=parsed, note=f"{files} list file(s) from browser harvest")
    return n
