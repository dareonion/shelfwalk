"""Wall Street Journal — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import os
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Wall Street Journal --------------------------------------------------------

# Subscriber harvest, same shape as NYT: harvest/wsj/<slug>.json holding
#   {list, year, url, books: [{title, author, publisher}]}
# WSJ's year-end article is regular — a title line followed by
# "By <Author> | <Publisher>" — so the in-page extractor is a two-line rule.
WSJ_DIR = os.path.join(HARVEST_DIR, "wsj")
_WSJ_LABELS = {"wsj-10-best": "The 10 Best Books"}


def load_wsj(conn) -> int:
    if not os.path.isdir(WSJ_DIR):
        db.log_fetch(conn, "wsj", False,
                     note=f"{WSJ_DIR}/ missing — Chrome pass needed")
        raise FileNotFoundError(f"{WSJ_DIR}/<slug>.json not found. "
                                "See docs/harvesting.md.")
    n = files = parsed = 0
    for fn in sorted(os.listdir(WSJ_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(WSJ_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        slug = payload.get("list") or fn[:-5]
        label = _WSJ_LABELS.get(slug, slug)
        year = payload.get("year")
        year = int(year) if str(year).isdigit() else None
        files += 1
        parsed += len(payload.get("books", []))
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            # 'Anjet Daanje, translated by David McKay' -> the author
            author = clean_nyt_author(b.get("author"))
            key = db.upsert_work(conn, title, author or None)
            if db.add_accolade(conn, key, "wsj", "list", "listed",
                               category=label, year=year,
                               detail=(b.get("publisher") or None),
                               url=payload.get("url")):
                n += 1
        conn.commit()
    db.log_fetch(conn, "wsj", files > 0, url="https://www.wsj.com/arts-culture/books",
                 n_records=n, n_parsed=parsed, note=f"{files} list file(s) from browser harvest")
    return n
