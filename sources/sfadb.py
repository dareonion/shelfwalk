"""sfadb — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- sfadb (Hugo / Nebula / Locus, incl. short fiction) -------------------------

# sfadb is the Locus Index to SF Awards: one site with complete winner AND
# nominee lists for all three awards, every category, back to each award's
# first year. The official sites are patchier and three separate scrapes.
SFADB_AWARDS = {
    "hugo":   ("Hugo Awards", "Hugo_Awards", 1953),
    "nebula": ("Nebula Awards", "Nebula_Awards", 1966),
    "locus":  ("Locus Awards", "Locus_Awards", 1971),
}

# Only written work. The dropped categories are people and productions —
# Best Editor, Best Dramatic Presentation, Best Fan Artist — which are real
# awards but not things you can borrow.
_SFADB_FORMS = {
    "novel": "novel", "first novel": "novel", "novella": "novella",
    "novelette": "novelette", "short story": "short-story",
    "short fiction": "short-story", "collection": "collection",
    "anthology": "anthology", "poem": "poetry", "long poem": "poetry",
    "short poem": "poetry", "nonfiction": "nonfiction",
    "non-fiction": "nonfiction", "related work": "nonfiction",
    "related book": "nonfiction", "young adult book": "novel",
    "ya book": "novel", "horror novel": "novel", "fantasy novel": "novel",
    "science fiction novel": "novel", "graphic story": "collection",
    "novelette/novella": "novella",
}

_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
# Short fiction titles are quoted, novels are bolded — see parse_sfadb_year.
_SFADB_QUOTED_RE = re.compile("[\u201c\"](.+?)[\u201d\"]", re.S)
_CATBLOCK_SPLIT = '<div class="categoryblock">'
_CAT_RE = re.compile(r'<div class="category">(.*?)</div>', re.S | re.I)


def _sfadb_form(category: str) -> str | None:
    c = re.sub(r"\s+", " ", (category or "").strip().lower())
    c = re.sub(r"^best\s+", "", c)
    for suffix in (" (tie)", ":"):
        c = c.replace(suffix, "")
    return _SFADB_FORMS.get(c.strip())


def parse_sfadb_year(page: str) -> list[dict]:
    """One sfadb year page -> entries.

    Winners carry `<span class="winner">`; everything else in the same list is
    a nominee. Title is the `<b>`, author the first `<a>`, publisher the
    trailing parenthetical.
    """
    out = []
    for chunk in page.split(_CATBLOCK_SPLIT)[1:]:
        m = _CAT_RE.search(chunk)
        if not m:
            continue
        category = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        form = _sfadb_form(category)
        if form is None:
            continue                       # people and productions
        body = chunk[m.end():]
        for li in _LI_RE.findall(body):
            is_winner = 'class="winner"' in li
            # ⚠ Short fiction is QUOTED, novels are BOLDED:
            #   <b>The Tusks of Extinction</b>, <a>Ray Nayler</a> (Tordotcom)
            #   "Better Living Through Algorithms", <a>Naomi Kritzer</a> (Clarkesworld)
            # Looking only for <b> dropped every quoted story, and where the
            # containing anthology happened to be bolded it captured THAT as
            # the title — so "Galaxy's Edge Vol. 13" was filed as a Hugo
            # short-story nominee while Kritzer's winner was absent entirely.
            # Strip tags BEFORE hunting for quotes: unescaping first leaves
            # the straight quotes of class="winner" in play, and the title
            # regex happily matched the word 'winner'.
            plain = _strip(htmlunescape(re.sub(r"<[^>]+>", " ", li)))
            plain = re.sub(r"^\s*Winner\s*:\s*", "", plain, flags=re.I)
            tm = re.search(r"<b>(.*?)</b>", li, re.S)
            qm = _SFADB_QUOTED_RE.search(plain)
            if qm:
                title = _strip(qm.group(1))
                after = plain[qm.end():]
            elif tm:
                title = _strip(htmlunescape(re.sub(r"<[^>]+>", " ", tm.group(1))))
                idx = plain.find(title)
                after = plain[idx + len(title):] if idx >= 0 else plain
            else:
                continue
            if not title:
                continue
            am = re.search(r"<a\b[^>]*>(.*?)</a>", li, re.S)
            author = _strip(htmlunescape(am.group(1))) if am else None
            tail = _strip(after)
            pm = re.search(r"\(([^()]*)\)\s*$", tail)
            out.append({"category": category, "form": form, "title": title,
                        "author": author,
                        "publisher": _strip(pm.group(1)) if pm else None,
                        "status": "winner" if is_winner else "nominee"})
    return out


def _load_sfadb(conn, award_key: str) -> int:
    import bayarea_lookup as B
    B.set_archive(conn.execute("PRAGMA database_list").fetchone()[2])
    _label, slug, first_year = SFADB_AWARDS[award_key]
    import datetime
    this_year = datetime.date.today().year
    n = years_ok = parsed = 0
    fails: list = []
    for year in range(first_year, this_year + 1):
        url = f"https://www.sfadb.com/{slug}_{year}"
        raw = _fetch(conn, url, fails)
        if raw is None:
            continue                                    # award not held / no page
        page = _decode_page(raw)
        entries = parse_sfadb_year(page)
        parsed += len(entries)
        if entries:
            years_ok += 1
        for e in entries:
            key = db.upsert_work(conn, e["title"], e["author"])
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (e["form"], key))
            if db.add_accolade(conn, key, award_key, "award", e["status"],
                               category=e["category"], year=year,
                               detail=e["publisher"], url=url):
                n += 1
        conn.commit()
    _warn_if_mostly_failing(award_key, years_ok, fails)
    db.log_fetch(conn, award_key, years_ok > 0, url=f"https://www.sfadb.com/{slug}",
                 n_records=n, n_parsed=parsed, note=_fetch_note(years_ok, fails, "years from sfadb"))
    return n
