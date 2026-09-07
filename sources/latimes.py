"""LA Times Book Prizes — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- LA Times Book Prizes -------------------------------------------------------

# latimes.com/events/festival-of-books/book-prizes. Fifteen categories, each an
# <h2 data-element="element-header-title">, with entries as rich-text modules
# holding three short lines: title, author, publisher. The winner's module
# carries `bp-winner-ribbon` in its class list.
#
# The page shows the current cycle; a "History" section links prior years.
LATIMES_URL = ("https://www.latimes.com/events/festival-of-books/book-prizes")
_LAT_H2_RE = re.compile(
    r'<h2[^>]*data-element="element-header-title"[^>]*>(.*?)</h2>', re.S)
_LAT_MOD_SPLIT = '<div data-element="rich-text-module"'
_LAT_FORMS = {"fiction": "novel", "first fiction": "novel",
              "biography": "nonfiction", "history": "nonfiction",
              "current interest": "nonfiction", "science": "nonfiction",
              "technology": "nonfiction", "poetry": "poetry",
              "graphic novel": "collection", "comics": "collection",
              "mystery": "novel", "thriller": "novel",
              "sci-fi": "novel", "fantasy": "novel",
              "young adult": "novel", "audiobook": "audio"}


def _lat_lines(module_html: str) -> list[str]:
    """Visible text of one module, minus images and their srcset noise."""
    body = module_html.split(">", 1)[1] if ">" in module_html else module_html
    body = re.sub(r"<(script|style|figure|picture)\b.*?</\1>", " ", body,
                  flags=re.S | re.I)
    body = re.sub(r"<img[^>]*>", " ", body)
    out = []
    for piece in re.split(r"<[^>]+>", body):
        piece = _strip(piece)
        if piece and "srcset" not in piece and len(piece) < 200:
            out.append(piece)
    return out


def parse_latimes(page: str) -> list[dict]:
    """-> [{category, title, author, publisher, status}] for the shown year."""
    parts = _LAT_H2_RE.split(page)
    out = []
    # parts alternates: [pre, heading, body, heading, body, ...]
    for idx in range(1, len(parts) - 1, 2):
        category = _strip(htmlunescape(parts[idx]))
        section = parts[idx + 1]
        if not category:
            continue
        # The winner's ribbon is its OWN image-only module sitting just before
        # the winner's text module, so the flag has to carry forward rather
        # than be read off the entry itself.
        pending_winner = False
        for mod in section.split(_LAT_MOD_SPLIT)[1:]:
            if "bp-winner-ribbon" in mod[:400]:
                pending_winner = True
            lines = _lat_lines(mod[:4000])
            # an entry is title / author / (publisher); anything else on the
            # page is prose, a sponsor logo or a section lead
            if len(lines) < 2 or len(lines[0]) > 140:
                continue
            if any(len(x) > 160 for x in lines[:2]):
                continue
            title = htmlunescape(lines[0])
            # 'Judges:' introduces the panel, not a book
            if title.rstrip().endswith(":") or re.match(r"judges\b", title, re.I):
                continue
            out.append({
                "category": category,
                "title": title,
                "author": htmlunescape(lines[1]),
                "publisher": htmlunescape(lines[2]) if len(lines) > 2 else None,
                "status": "winner" if pending_winner else "finalist",
            })
            pending_winner = False
    return out


def _lat_form(category: str) -> str:
    c = re.sub(r"\s+", " ", (category or "")).strip().lower()
    for key, form in _LAT_FORMS.items():
        if key in c:
            return form
    return "novel"


# The history page carries the whole run since 1980 on one 636KB page. Year
# markers and category headers alternate as flat text, so each category block
# binds to the most recent preceding year:
#   >2010<  ──────<br><b>FICTION</b><br>──────
#           <b>Winner: Ibis: A Novel</b>, Justin Haynes, Harry N. Abrams
#           <b>Finalists:</b><ul><li><b>Plum</b>, Andy Anderegg, Hub City</li>…
LATIMES_HISTORY = ("https://www.latimes.com/events/festival-of-books/"
                   "book-prizes/history")
_LATH_YEAR_RE = re.compile(r">\s*((?:19[89]|20[0-2])\d)\s*<")
_LATH_CAT_RE = re.compile(r"──+<br>\s*<b>(.*?)</b>\s*<br>──+", re.S)
# 'Winner:' sits inside the bold on most rows and outside it on a few
_LATH_WINNER_RE = re.compile(
    r"<b>\s*Winner\s*:?\s*(.*?)</b>\s*:?\s*,?\s*([^<]*)", re.S | re.I)
_LATH_FINALIST_RE = re.compile(r"<li>\s*<b>(.*?)</b>\s*,?\s*([^<]*)", re.S)


def _lath_credit(tail: str) -> str | None:
    """'Justin Haynes, Harry N. Abrams' -> the author (publisher dropped).

    The audiobook category credits narrators and producers rather than authors,
    so its 'author' is a production credit; that is the source's shape, not a
    parse error, and it is left as-is rather than guessed at.
    """
    tail = _strip(htmlunescape(tail)).strip(" ,:;")
    if not tail:
        return None
    return tail.split(",")[0].strip(" ,:;") or None


def parse_latimes_history(page: str) -> list[dict]:
    marks = [("year", m.start(), m.group(1), m.end())
             for m in _LATH_YEAR_RE.finditer(page)]
    marks += [("cat", m.start(), _kp_title_case(m.group(1)), m.end())
              for m in _LATH_CAT_RE.finditer(page)]
    marks.sort(key=lambda x: x[1])

    out, year = [], None
    for i, (kind, _start, value, end) in enumerate(marks):
        if kind == "year":
            year = int(value)
            continue
        if year is None or not value:
            continue
        block = page[end:marks[i + 1][1]] if i + 1 < len(marks) else page[end:]
        wm = _LATH_WINNER_RE.search(block)
        if wm:
            title = _strip(htmlunescape(re.sub(r"^\s*Winner\s*:?\s*", "",
                                               wm.group(1), flags=re.I)))
            if title:
                out.append({"year": year, "category": value, "title": title,
                            "author": _lath_credit(wm.group(2)),
                            "status": "winner"})
        # finalists only inside the <ul> that follows the Finalists label
        fi = re.search(r"Finalists?\s*:?\s*</b>?(.*?)</ul>", block, re.S | re.I)
        if fi:
            for t, tail in _LATH_FINALIST_RE.findall(fi.group(1)):
                title = _strip(htmlunescape(t))
                if title:
                    out.append({"year": year, "category": value,
                                "title": title, "author": _lath_credit(tail),
                                "status": "finalist"})
    return out


def load_latimes(conn) -> int:
    """History page first: it carries 1980 onward, where the prizes landing
    page carries only the current cycle."""
    fails: list = []
    hist = _fetch(conn, LATIMES_HISTORY, fails, timeout=90)
    if hist is not None:
        entries = parse_latimes_history(hist.decode("utf-8", "replace"))
        n = 0
        for e in entries:
            if not e["title"]:
                continue
            key = db.upsert_work(conn, e["title"], e["author"])
            conn.execute("UPDATE works SET form = COALESCE(form, ?) "
                         "WHERE work_key = ?", (_lat_form(e["category"]), key))
            if db.add_accolade(conn, key, "latimes", "award", e["status"],
                               category=e["category"], year=e["year"],
                               url=LATIMES_HISTORY):
                n += 1
        conn.commit()
        yrs = {e["year"] for e in entries}
        db.log_fetch(conn, "latimes", True, url=LATIMES_HISTORY, n_records=n,
                     n_parsed=len(entries),
                     note=_fetch_note(len(entries), fails,
                                      f"entries across {len(yrs)} years "
                                      f"({min(yrs)}-{max(yrs)})" if yrs else "entries"))
        return n

    raw = _fetch(conn, LATIMES_URL, fails, timeout=60)
    if raw is None:
        _warn_if_mostly_failing("latimes", 0, fails)
        db.log_fetch(conn, "latimes", False, url=LATIMES_URL,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    page = raw.decode("utf-8", "replace")
    ym = re.search(r"(\d{4})\s+Winners", page)
    year = int(ym.group(1)) if ym else None
    entries = parse_latimes(page)
    n = 0
    for e in entries:
        key = db.upsert_work(conn, e["title"], e["author"])
        conn.execute("UPDATE works SET form = COALESCE(form, ?) "
                     "WHERE work_key = ?", (_lat_form(e["category"]), key))
        if db.add_accolade(conn, key, "latimes", "award", e["status"],
                           category=e["category"], year=year,
                           detail=e["publisher"], url=LATIMES_URL):
            n += 1
    conn.commit()
    db.log_fetch(conn, "latimes", bool(entries), url=LATIMES_URL, n_records=n,
                 n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails,
                                  f"entries for {year} (current cycle only)"))
    return n
