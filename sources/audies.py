"""Audie Awards — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Audie Awards ---------------------------------------------------------------

# The Audio Publishers Association's prize, 1996 onward — the dedicated
# audiobook award. NOTE theaudies.com is a parked domain serving Kirkus
# content; audiopub.org is the real site.
#
# Slugs are inconsistent and the winners hub does not link every year: 2013,
# 2021 and 2022 are absent from it and were recovered from sitemap.xml. Hence
# an explicit map rather than a pattern.
#
# Page shape (Squarespace), flattened to text:
#   <CATEGORY> WINNER / <CATEGORY> FINALISTS
#   <title> ( Audio ) <credit> Published by <publisher>
# The credit is what makes this source worth having a narrator column for:
# "Written and narrated by Barbra Streisand" vs "Narrated by Sophie Amoss"
# with no author named at all.
AUDIE_BASE = "https://www.audiopub.org/"
AUDIE_YEARS = {
    **{y: f"{y}-audies-1" for y in range(1996, 2013) if y != 2000},
    2000: "2000-audies-award-1",
    2013: "2013-audies-2",
    **{y: f"{y}-audies-1" for y in range(2014, 2021)},
    2021: "2021-audie-awards-1",
    2022: "2022audieawards-1",
    2023: "2023audieawards-winners-1",
    2024: "2024audieawards-winners",
    2025: "2025audies-1",
    2026: "2026audieawards-winners",
}
_AU_HDR_RE = re.compile(r"^(.{3,60}?)\s+(WINNER|FINALISTS?)$")
_AU_NOISE = {"(", ")", "Audio", "AudioFile Review", "|", "Ebook", "Print", "-"}
_AU_NARRATED_RE = re.compile(
    r"(?:^|,\s*)(?:written\s+and\s+)?narrated by\s+(.+)$", re.I)
_AU_CREDIT_RE = re.compile(r"narrated by|^by\s|^written by", re.I)


def _audie_lines(page: str) -> list[str]:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    out = []
    for chunk in re.sub(r"<[^>]+>", "\n", body).split("\n"):
        chunk = re.sub(r"\s+", " ", htmlunescape(chunk)).strip()
        if chunk:
            out.append(chunk)
    return out


def parse_audie_year(page: str) -> list[dict]:
    """-> [{category, status, title, author, narrator}] for one Audie year."""
    out, category, status, pend = [], None, None, []
    for line in _audie_lines(page):
        m = _AU_HDR_RE.match(line)
        if m and m.group(1) == m.group(1).upper():
            category = _kp_title_case(m.group(1))
            status = "winner" if m.group(2).upper() == "WINNER" else "finalist"
            pend = []
            continue
        if category is None:
            continue
        if not line.startswith("Published by"):
            pend.append(line)
            continue
        credit = next((x for x in reversed(pend) if _AU_CREDIT_RE.search(x)), None)
        title = next((x for x in pend
                      if x not in _AU_NOISE and x is not credit and len(x) > 1), None)
        pend = []
        if not title or not credit:
            continue
        nm = _AU_NARRATED_RE.search(credit)
        narrator = nm.group(1).strip() if nm else None
        author = re.sub(r",?\s*(?:and\s+)?narrated by.*$", "", credit, flags=re.I)
        author = re.sub(r"^(?:written\s+)?by\s+", "", author, flags=re.I).strip(" ,")
        if re.match(r"^written and narrated by", credit, re.I):
            author = narrator          # the author read their own book
        out.append({"category": category, "status": status, "title": title,
                    "author": author or None, "narrator": narrator})
    return out


# Pages up to ~2016 use a different shape with NO "Published by" line — the
# publisher is a parenthetical on the narrator line — so the modern parser
# terminates no entries at all and silently yields nothing for those years:
#   winner    <title> / by <Author> / Narrated by <Narrator> (<Publisher>)
#   finalist  <Title> by <Author>; narrated by <Narrator> (<Publisher>)
_AU_ONELINE_RE = re.compile(
    r"^(.{2,120}?)\s+by\s+(.{2,80}?)\s*;\s*narrated by\s+(.{2,120}?)\s*\(", re.I)
_AU_NARRLINE_RE = re.compile(r"^Narrated by\s+(.{2,140}?)\s*(?:\(|$)", re.I)
_AU_BYLINE_RE = re.compile(r"^by\s+(.{2,90})$", re.I)


def parse_audie_year_legacy(page: str) -> list[dict]:
    lines = _audie_lines(page)
    out, category, status = [], None, None
    for i, line in enumerate(lines):
        m = _AU_HDR_RE.match(line)
        if m and m.group(1) == m.group(1).upper():
            category = _kp_title_case(m.group(1))
            status = "winner" if m.group(2).upper() == "WINNER" else "finalist"
            continue
        if category is None:
            continue
        one = _AU_ONELINE_RE.match(line)
        if one:
            out.append({"category": category, "status": status,
                        "title": _strip(one.group(1)),
                        "author": _strip(one.group(2)) or None,
                        "narrator": _strip(one.group(3)) or None})
            continue
        nm = _AU_NARRLINE_RE.match(line)
        if nm and i >= 2:
            bm = _AU_BYLINE_RE.match(lines[i - 1])
            if bm:
                title = _strip(lines[i - 2])
                if title and title not in _AU_NOISE and len(title) > 1:
                    out.append({"category": category, "status": status,
                                "title": title,
                                "author": _strip(bm.group(1)) or None,
                                "narrator": _strip(nm.group(1)) or None})
    return out


def parse_audie_any(page: str) -> list[dict]:
    """Modern layout first, legacy as the fallback — a year yielding nothing
    from one shape is a layout difference, not an empty year."""
    modern = parse_audie_year(page)
    return modern if modern else parse_audie_year_legacy(page)


def load_audies(conn) -> int:
    fails: list = []
    n = years_ok = parsed = 0
    for year, slug in sorted(AUDIE_YEARS.items()):
        raw = _fetch(conn, AUDIE_BASE + slug, fails, timeout=90)
        if raw is None:
            continue
        entries = parse_audie_any(raw.decode("utf-8", "replace"))
        parsed += len(entries)
        if entries:
            years_ok += 1
        for e in entries:
            key = db.upsert_work(conn, e["title"], e["author"])
            conn.execute("UPDATE works SET form = COALESCE(form, 'audio') "
                         "WHERE work_key = ?", (key,))
            if db.add_accolade(conn, key, "audies", "award", e["status"],
                               category=e["category"], year=year,
                               narrator=e["narrator"], url=AUDIE_BASE + slug):
                n += 1
        conn.commit()
    _warn_if_mostly_failing("audies", years_ok, fails)
    db.log_fetch(conn, "audies", years_ok > 0, url=AUDIE_BASE, n_records=n,
                 n_parsed=parsed,
                 note=_fetch_note(years_ok, fails, "years"))
    return n
