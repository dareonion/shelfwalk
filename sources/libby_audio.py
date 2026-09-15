"""Libby/OverDrive audiobook demand — see acclaim_core for the shared machinery."""
from __future__ import annotations

import datetime
import json
import re

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)
from sources.apple_audio import clean_apple_title, first_author

# --- Libby / OverDrive audiobook demand -----------------------------------------

# OverDrive's public Thunder API lists a library's whole collection with no
# key. sortBy=popularity is OverDrive-wide demand across every library — the
# same ordering for SCCL as for any other system — not local circulation.
# (sortBy=mostpopular-site is the per-library ordering; it is not used here.)
#
# Each item carries title/subtitle, creators with roles (Author, Narrator, …),
# languages, subjects and ratings.maturityLevel (generalcontent, adultonly,
# juvenile, youngadult). Juvenile and young-adult titles and anything without
# English are skipped, as is any title filed under a Juvenile or Young Adult
# subject. Rank is the position in the full OverDrive-wide ordering.
LIBBY_URL = ("https://thunder.api.overdrive.com/v2/libraries/santaclara/media"
             "?mediaTypes=audiobook&perPage=100&page={}&sortBy=popularity")
LIBBY_CHART = "Popularity (OverDrive-wide)"
LIBBY_LIMIT = 300          # adult English titles to keep
LIBBY_MAX_PAGES = 6
_LB_YOUTH_LEVELS = {"juvenile", "youngadult"}
# Long recordings are split into parts: 'Fourth Wing, Part 1 of 2'.
_LB_PART_RE = re.compile(r",?\s*Part\s+\d+\s+of\s+\d+\s*$", re.I)


def libby_is_adult_english(item: dict) -> bool:
    level = ((item.get("ratings") or {}).get("maturityLevel") or {}).get("id")
    if level in _LB_YOUTH_LEVELS:
        return False
    if not any(l.get("id") == "en" for l in item.get("languages") or []):
        return False
    return not any(("juvenile" in (s.get("name") or "").lower()
                    or "young adult" in (s.get("name") or "").lower())
                   for s in item.get("subjects") or [])


def parse_libby_page(payload: str | bytes, first_rank: int = 1) -> list[dict]:
    """-> [{rank, title, subtitle, author, narrators, isbn, adult_english}] for
    every item on one page, in order."""
    out = []
    for i, item in enumerate(json.loads(payload).get("items", [])):
        creators = item.get("creators") or []
        authors = [c.get("name") for c in creators if c.get("role") == "Author"]
        isbn = next((f.get("isbn") for f in item.get("formats") or [] if f.get("isbn")),
                    None)
        out.append({
            "rank": first_rank + i,
            "title": _LB_PART_RE.sub("", clean_apple_title(_strip(item.get("title")))),
            "subtitle": _strip(item.get("subtitle")) or None,
            "author": first_author(authors[0] if authors
                                   else item.get("firstCreatorName")),
            "narrators": [c.get("name") for c in creators if c.get("role") == "Narrator"],
            "isbn": isbn,
            "adult_english": libby_is_adult_english(item),
        })
    return out


def load_libby_audio(conn) -> int:
    fails: list = []
    today = datetime.date.today()
    kept, parsed, pages = [], 0, 0
    for page in range(1, LIBBY_MAX_PAGES + 1):
        raw = _fetch(conn, LIBBY_URL.format(page), fails, accept="application/json",
                     timeout=60, fresh=True)
        if raw is None:
            break
        rows = parse_libby_page(raw, first_rank=(page - 1) * 100 + 1)
        if not rows:
            break
        pages += 1
        parsed += len(rows)
        kept += [r for r in rows if r["adult_english"] and r["title"]]
        if len(kept) >= LIBBY_LIMIT:
            break
    kept = kept[:LIBBY_LIMIT]
    n = 0
    for e in kept:
        key = db.upsert_work(conn, e["title"], e["author"], subtitle=e["subtitle"],
                             isbns=[e["isbn"]] if e["isbn"] else None)
        if db.add_accolade(conn, key, "libby-audio", "popularity", "listed",
                           category=LIBBY_CHART, year=today.year,
                           detail=f"rank {e['rank']} on {today.isoformat()}",
                           narrator=", ".join(e["narrators"]) or None,
                           url=LIBBY_URL.format(1)):
            n += 1
    conn.commit()
    db.log_fetch(conn, "libby-audio", bool(kept), url=LIBBY_URL.format(1),
                 n_records=n, n_parsed=len(kept),
                 note=_fetch_note(pages, fails, f"pages, {parsed} titles scanned"))
    return n
