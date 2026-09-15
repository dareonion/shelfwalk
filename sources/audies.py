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
# audiobook award. theaudies.com is a parked domain serving Kirkus content;
# audiopub.org is the real site.
#
# Slugs are inconsistent and the winners hub does not link every year (2013,
# 2021 and 2022 appear only in sitemap.xml), hence an explicit map.
#
# Every year page lists each category as `<CATEGORY> WINNER` then
# `<CATEGORY> FINALISTS`, each entry a title followed by credit lines. The
# credits come in several shapes, all handled by one parser:
#   1996–2016  <title> / by <Author> / Narrated by <Narrator> (<Publisher>)
#              <title> / by <Author>; narrated by <Narrator> (<Publisher>)
#              <title> / written and read by <Author> (<Publisher>)
#   2017–2021  <title> / by <Author>, narrated by <Narrator>, published by <P>
#   2022–      <title> [( Audio )] / By <Author> / Narrated by <Narrator> /
#              Published by <Publisher> [/ AudioFile Review]
#   narrator categories, 2022–: <Narrator> / for / <title> / By <Author> / ...
# 2023 shouts titles and names in capitals.
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
_AU_HDR_RE = re.compile(r"^(.{3,80}?)\s+(WINNERS?|FINALISTS?)$")
_AU_NOT_CATEGORY = {"PAST"}                       # the site nav's "PAST WINNERS"
# honours for people and companies rather than recordings, headed either as a
# category ("SPECIAL ACHIEVEMENT AWARD WINNER") or on their own ("HALL OF FAME AWARD")
_AU_PERSON_CATEGORY_RE = re.compile(r"special achievement|hall of fame", re.I)
_AU_OTHER_HDR_RE = re.compile(r"^[A-Z0-9 ,’'&/:.-]{6,} AWARD$")
_AU_END = "APA LINKS"                             # footer after the last category
_AU_NOISE = {"(", ")", "Audio", "AudioFile Review", "|", "Ebook", "Print", "-"}
_AU_ROLES = (r"written|write|narrated|read|performed|created|adapted|edited|compiled|selected|"
             r"dramati[sz]ed|presented|translated|published|produced|directed|"
             r"illustrated|abridged|designed")
# "by X", "Written and narrated by X", "Edited, narrated and published by X"
_AU_ROLE_BY_RE = re.compile(
    rf"^((?:(?:{_AU_ROLES})\b[\s,]*(?:and\s+)?)*)by\b\s*(.*)$", re.I)
_AU_CREDIT_LINE_RE = re.compile(
    rf"^[,;\s]*(?:(?:(?:{_AU_ROLES})\b[\s,]*(?:and\s+)?)*by\b|package design\b)"
    rf"|[,;:]\s*(?:narrated|performed|read|published|edited) by\b", re.I)
_AU_PUBLISHED_RE = re.compile(r"^Published(?:\s+by)?\s", re.I)
# a credit splits into clauses at ';' and before a role phrase
_AU_CLAUSE_SPLIT_RE = re.compile(
    r"\s*;\s*"
    r"|,\s*(?=(?:and\s+)?(?:written|narrated|performed|read|published|adapted|"
    r"edited|foreword|afterword|introduction|translated|dramati[sz]ed|presented|"
    r"design(?:ed)?|package design|with|featuring|from the|based on|music|"
    r"abridged|illustrated|compiled|script)\b)"
    r"|(?<!and)\s+(?=published by\b)"
    r"|[:.]\s+(?=(?:narrated|performed|read) by\b)", re.I)
_AU_ONELINE_RE = re.compile(
    r"^(.{2,120}?)\s+by\s+(.{2,80}?)\s*;\s*narrated by\s+(.{2,120}?)\s*\(", re.I)
_AU_TITLE_BY_RE = re.compile(r"^(.+?)\s+by\s+([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3})$")
# category names the pages misspell or vary between the winner and finalist
# headers of the same category
_AU_CATEGORY_FIX = {"Audiobook The Year": "Audiobook Of The Year",
                    "Audiobook Drama": "Audio Drama"}


def _audie_lines(page: str) -> list[str]:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    out = []
    for chunk in re.sub(r"<[^>]+>", "\n", body).split("\n"):
        chunk = re.sub(r"\s+", " ", htmlunescape(chunk)).strip()
        if chunk:
            out.append(chunk)
    return out


def _title_case(s: str) -> str:
    """Fold an all-caps string to title case, keeping accents and apostrophes
    ('WOMEN’S' -> 'Women’s', 'NÉMETH' -> 'Németh'); mixed case is left alone."""
    s = (s or "").strip()
    if not s or s != s.upper():
        return s
    return re.sub(r"[^\W\d_]+(?:['’][^\W\d_]+)?",
                  lambda m: m.group(0)[0].upper() + m.group(0)[1:], s.lower())


def _header(line: str):
    m = _AU_HDR_RE.match(line)
    if not m or m.group(1) != m.group(1).upper() or m.group(1) in _AU_NOT_CATEGORY:
        return None
    category = _title_case(m.group(1)).strip(" -")
    category = _AU_CATEGORY_FIX.get(category, category)
    status = "winner" if m.group(2).startswith("WINNER") else "finalist"
    return category, status


def _clean_name(s: str) -> str | None:
    s = re.sub(r"\s*\([^()]*\)?[\s.]*$", "", s or "")    # trailing (Publisher)
    s = re.sub(r"^(?:(?:read|narrated)(?: by)?|by)\s+", "", s.strip(), flags=re.I)
    return _title_case(s.strip(" ,;.")) or None


def _split_credit(credit: str) -> tuple[str | None, str | None]:
    """'by X, narrated by Y, published by Z' and its variants -> (author, narrator).

    Roles before 'by' decide what the name is: written/created or none ->
    author; narrated/read/performed -> narrator; edited/adapted/compiled ->
    author only when no writer is named. '(written and narrated)' after a name
    makes the author the narrator too."""
    author = fallback = narrator = None
    credit = re.sub(rf"\b({_AU_ROLES})\s*,\s*(?=(?:{_AU_ROLES})\b)", r"\1 and ",
                    credit, flags=re.I)
    for clause in _AU_CLAUSE_SPLIT_RE.split(credit):
        c = clause.strip(" ,;")
        if not c:
            continue
        self_read = re.search(r"\((?:written|narrated) and (?:narrated|read|written)\)",
                              c, re.I)
        m = _AU_ROLE_BY_RE.match(c)
        if not m:
            m2 = re.match(r"^(?:narrated|performed|read)\s+(.+)$", c, re.I)
            if m2:
                narrator = narrator or _clean_name(m2.group(1))
            continue
        roles = {r.lower() for r in re.findall(_AU_ROLES, m.group(1), re.I)}
        name = _clean_name(re.sub(r"\s*\((?:written|narrated)[^)]*\)", "", m.group(2)))
        if not name:
            continue
        if not roles or roles & {"written", "write", "created", "edited", "adapted", "compiled",
                                 "selected", "dramatised", "dramatized"}:
            name = re.sub(r",?\s*(?:and\s+)?(?:a\s+)?full cast$", "", name) or name
        ensemble = re.match(r"^(?:a|an|the)\s+(?:full cast|chorus|authors?)\b", name, re.I)
        if (not roles or roles & {"written", "write", "created"}) and not ensemble:
            author = author or name
        if roles & {"narrated", "read", "performed"} or self_read:
            narrator = narrator or name
        if roles & {"edited", "adapted", "compiled", "selected", "dramatised",
                    "dramatized"} and not ensemble:
            fallback = fallback or name
    return author or fallback, narrator


def _parse_audie_lines(lines: list[str]) -> list[dict]:
    """One pass over a year page's lines -> [{category, status, title, author,
    narrator}]. An entry is a title, optional 'for' (narrator categories), and
    credit lines; it ends at 'Published by', a new title, or a header."""
    out: list[dict] = []
    category = status = None
    cur: dict = {}

    def flush():
        nonlocal cur
        title = _title_case((cur.get("title") or "").strip(" ,"))
        credits = cur.get("credits", [])
        if category and title and (credits or cur.get("named")):
            author, narrator = _split_credit(" ; ".join(credits))
            if cur.get("named"):                  # "<Narrator> for <title>"
                narrator = _clean_name(cur["named"])
            by = _AU_TITLE_BY_RE.match(title)
            if not author and by:                 # "No Name by Wilkie Collins"
                title, author = by.group(1), by.group(2)
            out.append({"category": category, "status": status, "title": title,
                        "author": author, "narrator": narrator})
        cur = {}

    def next_line(i):
        return next((x for x in lines[i + 1:] if x not in _AU_NOISE), "")

    for i, line in enumerate(lines):
        if line == _AU_END:
            break
        hdr = _header(line)
        if hdr or _AU_OTHER_HDR_RE.match(line):
            flush()
            category, status = hdr if hdr else (None, None)
            if category and _AU_PERSON_CATEGORY_RE.search(category):
                category = None
            continue
        if category is None or line in _AU_NOISE:
            continue
        if _AU_PUBLISHED_RE.match(line):
            flush()
            continue
        credits = cur.setdefault("credits", [])
        if not re.search(r"\w", line):            # stray "," or "!" on its own line
            if cur.get("title") and not credits:
                cur["title"] += line
            continue
        if line.lower() == "full cast" or (credits and re.search(
                r"(?:,|\band|\bwith a|\bby|\bof)$", credits[-1], re.I)):
            if credits:                           # a credit wrapped onto this line
                credits[-1] = f"{credits[-1]} {line}"
            continue
        if line.lower() == "for":
            cur["named"], cur["title"] = cur.get("title"), None
            continue
        tail = re.match(r"^([a-z][^,;]*?)\s+by\s+(.+;\s*narrated by.+)$", line)
        if cur.get("title") and not credits and tail and not _AU_CREDIT_LINE_RE.match(line):
            # "Coming of Age" / "in Mississippi by Anne Moody; narrated by ..."
            cur["title"] = f"{cur['title']} {tail.group(1)}"
            credits.append(f"by {tail.group(2)}")
            continue
        one = _AU_ONELINE_RE.match(line)
        if one and not _AU_CREDIT_LINE_RE.match(line):
            flush()                               # "<Title> by <A>; narrated by <N> (<P>)"
            cur = {"title": one.group(1),
                   "credits": [f"by {one.group(2)}; narrated by {one.group(3)}"]}
            continue
        if cur.get("title") and (_AU_CREDIT_LINE_RE.search(line)
                                 or line.startswith("from ")):
            credits.append(line)
            continue
        if cur.get("title") and credits and _AU_PUBLISHED_RE.match(next_line(i)):
            credits.append(f"narrated by {line}")  # an unlabelled cast list
            continue
        if (cur.get("title") and credits and re.search(r"\([^()]+\)$", line)
                and not re.search(r"narrat|read|perform", " ".join(credits), re.I)
                and not re.search(r"\bby\b", line)):
            credits.append(f"narrated by {line}")  # "by X" then "<Narrator> (<P>)"
            continue
        if _AU_CREDIT_LINE_RE.match(line):
            continue                              # a credit with no title before it
        if cur.get("title") and credits:
            flush()                               # the previous entry had no terminator
            cur = {"title": line}
        elif cur.get("title"):
            cur["title"] = f"{cur['title']} {line}"
        else:
            cur["title"] = line
    flush()
    return out


def parse_audie_year(page: str) -> list[dict]:
    """Pages laid out with a 'Published by' line per entry (2022 onward);
    [] for a page without one."""
    lines = _audie_lines(page)
    if not any(_AU_PUBLISHED_RE.match(x) for x in lines):
        return []
    return _parse_audie_lines(lines)


def parse_audie_year_legacy(page: str) -> list[dict]:
    """Any layout, including the publisher-in-parentheses pages before 2017."""
    return _parse_audie_lines(_audie_lines(page))


def parse_audie_any(page: str) -> list[dict]:
    return _parse_audie_lines(_audie_lines(page))


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
            # a recording's prize says nothing about the text's form, so none is set
            key = db.upsert_work(conn, e["title"], e["author"])
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
