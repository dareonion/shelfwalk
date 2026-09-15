"""ALA RUSA Listen List — see acclaim_core for the shared machinery."""
from __future__ import annotations

import re
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- The Listen List: Outstanding Audiobook Narration -----------------------------

# RUSA's juried list of about twelve audiobooks a year, chosen for the narration;
# the year is the announcement year (January, for the previous year's titles).
# Every selection carries three "Listen-Alikes", which are recommendations, not
# selections, and are never loaded.
#
# Three page shapes:
#   LISTEN_LIST_URL (redirects to rusaupdate.org/awards/the-listen-list/) holds
#     2016 onward as accordion panels, one per year:
#       <span class="ac_title_class">2026</span> … <div class="wpsm_panel-body">
#       <h3>“Title” [by Author]</h3>
#       <p>[Written by Author.] Narrated by N. Publisher. annotation</p>
#       <p><strong>Listen-Alikes:</strong></p> …
#     The credit paragraph can follow the annotation, and the author can be in
#     the heading, in "Written by", or absent.
#   LISTEN_LIST_ARCHIVE pages (2015 list, 2012–2014 press releases) mark each
#     selection as a paragraph opening in <strong>:
#       <p><strong>“Title,” by Author. Narrated by N.</strong> Publisher. …</p>
#     with Listen-Alikes as plain paragraphs or <li>.
#   The 2012–2015 panels on LISTEN_LIST_URL are empty pointers to those pages.
#
# ala.org robots.txt asks for Crawl-delay: 10. The pages are fixed URLs, fetched
# mirror-first, so a load touches the network only for pages not yet mirrored.
LISTEN_LIST_URL = "https://www.ala.org/rusa/awards/listenlist"
LISTEN_LIST_ARCHIVE = {
    2015: "https://www.ala.org/rusa/awards/listenlist/2015",
    2014: "https://www.ala.org/news/2012/04/"
          "rusas-2014-listen-list-announces-outstanding-audiobook-narration-award-winners",
    2013: "https://www.ala.org/news/2013/01/"
          "rusas-2013-listen-list-highlights-audiobooks-provide-extraordinary-listening",
    2012: "https://www.ala.org/news/press-releases/2012/01/"
          "rusas-inaugural-listen-list-outstanding-audiobook-narration-revealed",
}

_LL_QUOTES = "“”\"'‘’"
_LL_PANEL_RE = re.compile(
    r'class="ac_title_class">.*?(\d{4})\s*</span>(.*?)<!-- Inner panel End -->', re.S)
_LL_ALIKES_RE = re.compile(r"\bListen[\s-]*alikes?\b", re.I)
# "rby" and "by by" are typos on the page itself
_LL_HEAD_BY_RE = re.compile(
    r"[”\"’']\s*,?\s*(?:edited\s+)?r?by\s+(?:by\s+)?(.+?)\.?\s*$", re.I)
_LL_HEAD_NARR_RE = re.compile(r"\.?\s+(?:Narrated|Read)\s+by\s+", re.I)
_LL_WRITTEN_RE = re.compile(r"(?:^|[.!?]\s+)Written\s+(?:by\s+)?(.+)$", re.S)
_LL_NARRATED_RE = re.compile(r"\b(?:Narrated|Read)\s+by\s+(.+)$", re.S | re.I)
_LL_CREDIT_RE = re.compile(
    r"^\s*[“\"‘']\s*(?P<title>.+?)\s*,?\s*[”\"’']\s*,?\s*by\s+(?P<author>.+?)"
    r"\s*[.,]?\s+(?:Narrated|Read)\s+by\s+(?P<rest>.+)$", re.S | re.I)
# the opening quote sometimes sits just outside the <strong>
_LL_STRONG_P_RE = re.compile(
    r"<p\b[^>]*>((?:\s|&ldquo;|&quot;|[“\"])*<strong>.*?)</p>", re.S | re.I)
_LL_ABBREV = {"Jr", "Sr", "Dr", "Mr", "Mrs", "Ms", "St", "Lt"}
_LL_PUBLISHER_RE = re.compile(
    r"\b(?:Audio|Audiobooks?|AudioGO|Books on Tape|Recorded Books|Publishing|"
    r"Media|Studios|Macmillan|Hachette|Blackstone|Tantor|Brilliance|Dreamscape|"
    r"HighBridge|Podium|Penguin|Random House|Schuster|Naxos|Harlequin|Caedmon|"
    r"Sourcebooks)\b", re.I)


def _ll_text(html: str) -> str:
    return re.sub(r"\s+", " ", htmlunescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def _ll_sentence(s: str) -> str:
    """Text up to the first full stop that ends a sentence rather than an
    initial ('R.C. Bray.', 'M. R. Carey.') or an abbreviation."""
    for m in re.finditer(r"\.(?=\s|$)", s):
        token = s[:m.start()].split()[-1] if s[:m.start()].split() else ""
        if re.fullmatch(r"[A-Z]|(?:[A-Z]\.)+[A-Z]", token) or token in _LL_ABBREV:
            continue
        return s[:m.start()].strip()
    return s.strip()


def _ll_title(s: str) -> str:
    return s.strip().strip(_LL_QUOTES + " ,").strip()


def _ll_narrator(rest: str) -> str | None:
    """'Ron Butler, Macmillan Audio. …' -> 'Ron Butler'."""
    names = _ll_sentence(rest)
    parts = [p for p in re.split(r",\s*", names)]
    while len(parts) > 1 and _LL_PUBLISHER_RE.search(parts[-1]):
        parts.pop()
    return ", ".join(parts).strip() or None


def parse_listen_list_panels(page: str) -> list[dict]:
    """The rusaupdate.org page: 2016 onward, one accordion panel per year.
    -> [{year, title, author, narrator}]"""
    out = []
    for m in _LL_PANEL_RE.finditer(page):
        year = int(m.group(1))
        body = m.group(2).split('class="wpsm_panel-body">', 1)[-1]
        for block in re.split(r"<h3\b[^>]*>", body)[1:]:
            head_html, _, rest_html = block.partition("</h3>")
            heading = re.sub(r"\s+class$", "", _ll_text(head_html))
            rest = _ll_text(rest_html)
            cut = _LL_ALIKES_RE.search(rest)
            credit = rest[:cut.start()] if cut else rest
            head_narr = _LL_HEAD_NARR_RE.search(heading)
            if head_narr and not heading.startswith(tuple(_LL_QUOTES)):
                # an unquoted heading carrying a whole credit line is a pasted
                # Listen-Alike, and the paragraph under it belongs elsewhere
                continue
            narrator = None
            if head_narr:
                narrator = _ll_narrator(heading[head_narr.end():])
                heading = heading[:head_narr.start()]
            author = None
            by = _LL_HEAD_BY_RE.search(heading)
            if by:
                author = by.group(1).strip()
                heading = heading[:by.start() + 1]
            title = _ll_title(heading)
            if not title:
                continue
            if author is None:
                wm = _LL_WRITTEN_RE.search(credit)
                if wm:
                    author = _ll_sentence(wm.group(1)) or None
            if narrator is None:
                nm = _LL_NARRATED_RE.search(credit)
                narrator = _ll_narrator(nm.group(1)) if nm else None
            out.append({"year": year, "title": title, "author": author,
                        "narrator": narrator})
    return out


def parse_listen_list_archive(page: str, year: int) -> list[dict]:
    """A 2012–2015 page, where each selection opens with <strong>.
    -> [{year, title, author, narrator}]"""
    out = []
    for m in _LL_STRONG_P_RE.finditer(page):
        cm = _LL_CREDIT_RE.match(_ll_text(m.group(1)))
        if not cm:
            continue
        out.append({"year": year, "title": _ll_title(cm.group("title")),
                    "author": cm.group("author").strip(" ,.") or None,
                    "narrator": _ll_narrator(cm.group("rest"))})
    return out


def load_listen_list(conn) -> int:
    fails: list = []
    n = parsed = pages_ok = 0
    pages = [(LISTEN_LIST_URL, None)] + [(u, y) for y, u in sorted(LISTEN_LIST_ARCHIVE.items())]
    for url, year in pages:
        # the main page gains a panel each year; archived lists never change
        raw = _fetch(conn, url, fails, timeout=60, fresh=year is None)
        if raw is None:
            continue
        page = raw.decode("utf-8", "replace")
        entries = (parse_listen_list_panels(page) if year is None
                   else parse_listen_list_archive(page, year))
        parsed += len(entries)
        if entries:
            pages_ok += 1
        for e in entries:
            key = db.upsert_work(conn, e["title"], e["author"])
            if db.add_accolade(conn, key, "listen-list", "list", "listed",
                               category="Listen List", year=e["year"],
                               narrator=e["narrator"], url=url):
                n += 1
        conn.commit()
    _warn_if_mostly_failing("listen-list", pages_ok, fails)
    db.log_fetch(conn, "listen-list", pages_ok > 0, url=LISTEN_LIST_URL,
                 n_records=n, n_parsed=parsed,
                 note=_fetch_note(pages_ok, fails, "pages"))
    return n
