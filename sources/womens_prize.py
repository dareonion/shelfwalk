"""Women's Prize — see acclaim_core for the shared machinery."""
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

# --- Women's Prize --------------------------------------------------------------

# womensprize.com renders its library client-side, which is what defeated the
# first attempt — but it is WordPress, and /wp-json/wp/v2/book is wide open:
# 1,423 books with book_author / prize_year / prize_type taxonomies. Compare
# Wikidata's 6 nominees for this award.
#
# The API does not distinguish shortlist from longlist, but each book's own
# page carries the exact sentence — "Shortlisted for the 2026 Women's Prize for
# Fiction" — so the spine comes from the API and the status from the page. Both
# go through the raw mirror, so a re-run costs nothing.
WP_API = "https://womensprize.com/wp-json/wp/v2"
# The category group is an explicit alternation, not [A-Za-z -]+: a greedy
# class swallows the blurb that follows and yields 'Fiction Piranesi Lives'.
_WP_STATUS_RE = re.compile(
    r"\b(Winner of|Shortlisted for|Longlisted for)\s+the\s+(\d{4})\s+"
    r"Women'?s Prize(?:\s+for\s+(Non[- ]?Fiction|Fiction|Poetry))?", re.I)
_WP_STATUS_MAP = {"winner of": "winner", "shortlisted for": "shortlist",
                  "longlisted for": "longlist"}


def parse_womens_prize_status(page_text: str) -> dict | None:
    """The one sentence on a book page that says what it actually won."""
    m = _WP_STATUS_RE.search(re.sub(r"\s+", " ", page_text))
    if not m:
        return None
    return {"status": _WP_STATUS_MAP[m.group(1).lower()],
            "year": int(m.group(2)),
            "category": (m.group(3) or "Fiction").strip().title()}


def _wp_terms(conn, tax: str, fails: list) -> dict:
    """taxonomy term id -> name, one request per 100 terms."""
    out, page = {}, 1
    while True:
        raw = _fetch(conn, f"{WP_API}/{tax}?per_page=100&page={page}", fails,
                     accept="application/json", timeout=45)
        if raw is None:
            break
        terms = json.loads(raw)
        if not terms:
            break
        for t in terms:
            out[t["id"]] = t.get("name") or t.get("slug")
        if len(terms) < 100:
            break
        page += 1
    return out


def load_womens_prize(conn) -> int:
    fails: list = []
    authors = _wp_terms(conn, "book_author", fails)
    years = _wp_terms(conn, "prize_year", fails)
    types = _wp_terms(conn, "prize_type", fails)

    books, page = [], 1
    while True:
        raw = _fetch(conn, f"{WP_API}/book?per_page=100&page={page}"
                            "&_fields=id,title,slug,link,book_author,"
                            "prize_year,prize_type", fails,
                     accept="application/json", timeout=60)
        if raw is None:
            break
        chunk = json.loads(raw)
        if not chunk:
            break
        books += chunk
        if len(chunk) < 100:
            break
        page += 1

    n = with_status = 0
    for b in books:
        title = demojibake(htmlunescape(
            _strip((b.get("title") or {}).get("rendered", ""))))
        if not title:
            continue
        author = next((authors[t] for t in (b.get("book_author") or [])
                       if t in authors), None)
        # exact status from the book's own page; the API only knows "listed"
        rec = None
        raw = _fetch(conn, b.get("link") or "", fails, timeout=45) \
            if b.get("link") else None
        if raw is not None:
            rec = parse_womens_prize_status(
                re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace")))
        if rec:
            with_status += 1
            year, status, category = rec["year"], rec["status"], rec["category"]
        else:
            year = next((int(years[t]) for t in (b.get("prize_year") or [])
                         if t in years and str(years[t]).isdigit()), None)
            if year is None:
                continue                   # library extra, never on a list
            status = "listed"
            category = next((types[t] for t in (b.get("prize_type") or [])
                             if t in types), "Fiction")
        key = db.upsert_work(conn, title, author)
        conn.execute(
            "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
            ("nonfiction" if "non" in category.lower() else "novel", key))
        if db.add_accolade(conn, key, "womens-prize", "award", status,
                           category=f"Women's Prize for {category}",
                           year=year, url=b.get("link")):
            n += 1
        conn.commit()
    _warn_if_mostly_failing("womens-prize", len(books), fails)
    db.log_fetch(conn, "womens-prize", bool(books), url=f"{WP_API}/book",
                 n_records=n, n_parsed=len(books),
                 note=_fetch_note(len(books), fails,
                                  f"books via WP REST; {with_status} with an "
                                  f"exact status line"))
    return n
