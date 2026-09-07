"""FT Business Book of the Year — see acclaim_core for the shared machinery."""
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

# --- FT Business Book of the Year -----------------------------------------------

# ⚠ THE ONE WIKIPEDIA-SOURCED SOURCE, and deliberately so. The award's own page
# (ft.com/bookaward) is an index of articles whose bodies sit behind the FT
# paywall, and the shortlists live in those bodies. With no FT subscription
# there is no original route, so this falls back — and every record it writes
# is stamped 'via Wikipedia' in `detail` so the provenance is never ambiguous.
#
# Structure: '=== <year> ===' sections of bullets, winner flagged {{Blue ribbon}}:
#   * {{Blue ribbon}} [[Parmy Olson]], ''[[Supremacy (book)|Supremacy: AI…]]''
FT_WIKI_PAGE = "Financial Times Business Book of the Year Award"
FT_WIKI_API = ("https://en.wikipedia.org/w/api.php?action=parse&page={}"
               "&prop=wikitext&format=json")
# The lookahead must stop at ANY heading level. Stopping only at '\n===' let
# the final year section run on through the level-2 headings after it, which
# pulled a bullet out of a later section and filed it as a 2025 shortlistee.
_FT_YEAR_RE = re.compile(r"===+\s*(\d{4})\s*===+(.*?)(?=\n==|\Z)", re.S)
_FT_ITALIC_RE = re.compile(r"''+(.+?)''+", re.S)





def parse_ft_wikitext(wikitext: str) -> list[dict]:
    out = []
    for year, body in _FT_YEAR_RE.findall(wikitext):
        for line in body.split("\n"):
            line = line.strip()
            # Most years are bullet lists; 2020 is a wiki table, so its entries
            # start with '|' instead of '*' and the whole year was being
            # skipped. An italic title is still required below, which keeps
            # table headers and formatting rows out.
            if not line.startswith(("*", "|")):
                continue
            item = line.lstrip("*|").strip()
            # {{Blue ribbon}} in some years, {{blue ribbon}} in others — a
            # case-sensitive check silently lost 14 of 21 winners.
            status = ("winner" if re.search(r"\{\{\s*blue ribbon", item, re.I)
                      else "shortlist")
            tm = _FT_ITALIC_RE.search(item)
            if not tm:
                continue
            title = _wiki_plain(tm.group(1))
            author = _wiki_plain(item[:tm.start()])
            if not title:
                continue
            out.append({"year": int(year), "title": title,
                        "author": author or None, "status": status})
    return out


def load_ft_business(conn) -> int:
    fails: list = []
    url = FT_WIKI_API.format(FT_WIKI_PAGE.replace(" ", "%20"))
    raw = _fetch(conn, url, fails, accept="application/json", timeout=45)
    if raw is None:
        _warn_if_mostly_failing("ft-business", 0, fails)
        db.log_fetch(conn, "ft-business", False, url=url,
                     note=_fetch_note(0, fails, "pages"))
        return 0
    wikitext = json.loads(raw)["parse"]["wikitext"]["*"]
    entries = parse_ft_wikitext(wikitext)
    n = 0
    for e in entries:
        key = db.upsert_work(conn, e["title"], e["author"])
        conn.execute("UPDATE works SET form = COALESCE(form, 'nonfiction') "
                     "WHERE work_key = ?", (key,))
        if db.add_accolade(conn, key, "ft-business", "award", e["status"],
                           category="Business Book of the Year",
                           year=e["year"], detail="via Wikipedia (FT paywalled)",
                           url="https://www.ft.com/bookaward"):
            n += 1
    conn.commit()
    db.log_fetch(conn, "ft-business", bool(entries), url=url, n_records=n,
                 n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails,
                                  "entries — WIKIPEDIA FALLBACK, FT paywalled"))
    return n
