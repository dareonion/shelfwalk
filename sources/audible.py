"""Audible Best of the Year lists and bestseller charts — see acclaim_core for
the shared machinery."""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Audible --------------------------------------------------------------------

# Two sources from one site, both server-rendered:
#
# audible-best (editorial list)
#   /ep/best-of-the-year      current year only: Audiobook of the Year + Top 20.
#                             Each Top 20 item is an <a aria-label='Title by Author'
#                             href="/pd/…">; narrators are the searchNarrator links
#                             in that item's blurb.
#   /blog/tag/best-of-the-year[/page/N]
#                             every year's genre lists (2022 onward). Each article
#                             embeds one schema.org Audiobook JSON-LD block per
#                             pick: name, author, readBy, mainEntityOfPage.
#
# audible-charts (popularity snapshot)
#   /charts/best and /charts/best/<category>/<node>: top 20 per chart. robots.txt
#   disallows /charts/*? for every agent, so pagination (?page=2…) is never
#   fetched. Items are <li class="productListItem" aria-label='Title'> with
#   authorLabel / narratorLabel lists.
#
# robots.txt also disallows any URL carrying ?plink=, so every href is stripped
# of its query string before use.
AUDIBLE = "https://www.audible.com"
AUDIBLE_BOTY_HUB = AUDIBLE + "/ep/best-of-the-year"
AUDIBLE_BOTY_TAG = AUDIBLE + "/blog/tag/best-of-the-year"
AUDIBLE_CHART = AUDIBLE + "/charts/best"
# category charts worth a weekly snapshot, matched against the slugs linked
# from the overall chart
AUDIBLE_CHART_CATEGORIES = (
    "literature-fiction", "science-fiction-fantasy", "mystery-thriller-suspense",
    "biographies-memoirs", "history", "politics-social-sciences",
    "science-engineering")
# a page older than this is re-fetched; None = the mirror is final
HUB_MAX_AGE_DAYS = 30
CHART_MAX_AGE_DAYS = 6

_TAG_ARTICLE_RE = re.compile(
    r'href="(?:https://www\.audible\.com)?'
    r'(/blog/(?:article-(20\d\d)-best-[a-z0-9-]*audiobooks[a-z0-9-]*'
    r'|best-of-the-year-editorial-(20\d\d)))["?#]')
_TAG_PAGE_RE = re.compile(r'href="(?:https://www\.audible\.com)?'
                          r'/blog/tag/best-of-the-year/page/(\d+)"')
_LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_NARRATOR_A_RE = re.compile(
    r'href="[^"]*searchNarrator=[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]+?)\s*<')
_AUTHOR_A_RE = re.compile(
    r'href="/author/[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]+?)\s*<')
_PD_TITLE_A_RE = re.compile(
    r'href="/pd/[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]+?)\s*<')
_HUB_ITEM_RE = re.compile(r"aria-label='([^']+)' href=\"/pd/")


def _text(fragment: str) -> str:
    text = re.sub(r"\s+", " ", htmlunescape(re.sub(r"<[^>]+>", " ", fragment)))
    return re.sub(r"\s+([,;])", r"\1", text).strip(" ,;")


def _names(value) -> list[str]:
    """schema.org person fields arrive as a string, a list, or {name: …}."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("name")
    if isinstance(value, list):
        return [n for v in value for n in _names(v)]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _no_scripts(page: str) -> str:
    return re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)


# --- parsing --------------------------------------------------------------------

def parse_boty_hub(page: str) -> dict:
    """/ep/best-of-the-year -> {year, entries: [{category, title, author,
    narrator}]}: the Audiobook of the Year, then the Top 20."""
    m = re.search(r"<title>\s*The Best Audiobooks of (20\d\d)", page)
    year = int(m.group(1)) if m else None
    body = _no_scripts(page)
    entries = []
    a = body.find("Audiobook of the Year")
    b = body.find("Our Top 20")
    if a >= 0 and b > a:
        seg = body[a:b]
        title = _PD_TITLE_A_RE.search(seg)
        author = _AUTHOR_A_RE.search(seg)
        if title:
            entries.append({
                "category": "Audiobook of the Year", "title": _text(title.group(1)),
                "author": _text(author.group(1)) if author else None,
                "narrator": ", ".join(_text(n) for n in _NARRATOR_A_RE.findall(seg))
                            or None})
    if b >= 0:
        end = body.find("Browse our top picks", b)
        seg = body[b:end if end > b else len(body)]
        starts = [m for m in _HUB_ITEM_RE.finditer(seg)]
        for i, m in enumerate(starts):
            chunk = seg[m.start():starts[i + 1].start() if i + 1 < len(starts) else len(seg)]
            label = htmlunescape(m.group(1))
            author_a = _AUTHOR_A_RE.search(chunk)
            author = _text(author_a.group(1)) if author_a else None
            # the heading keeps punctuation the aria-label drops ('Someday, Now')
            heading = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
            if heading and _text(heading.group(1)):
                title = _text(heading.group(1))
                author = author or label.rpartition(" by ")[2] or None
            elif author and label.endswith(" by " + author):
                title = label[:-len(" by " + author)]
            else:
                title, _, by = label.rpartition(" by ")
                title, author = (title, author or by) if title else (label, author)
            entries.append({
                "category": "Top 20", "title": title.strip(), "author": author,
                "narrator": ", ".join(_text(n) for n in _NARRATOR_A_RE.findall(chunk))
                            or None})
    return {"year": year, "entries": entries}


def parse_boty_tag(page: str) -> dict:
    """A best-of-the-year tag page -> {articles: [(path, year)], last_page}."""
    seen, articles = set(), []
    for m in _TAG_ARTICLE_RE.finditer(page):
        path = m.group(1)
        if path in seen or path.endswith("-app-test"):
            continue
        seen.add(path)
        articles.append((path, int(m.group(2) or m.group(3))))
    pages = [int(p) for p in _TAG_PAGE_RE.findall(page)]
    return {"articles": articles, "last_page": max(pages, default=1)}


def parse_blog_list(page: str, path: str = "") -> dict:
    """A best-of article -> {year, list, entries: [{title, author, narrator, url}]}
    from its Audiobook JSON-LD blocks, in page order."""
    blocks = []
    for m in _LD_RE.finditer(page):
        try:
            blocks.append(json.loads(m.group(1)))
        except ValueError:
            continue
    article = next((b for b in blocks if isinstance(b, dict)
                    and b.get("@type") == "NewsArticle"), {})
    headline = (article.get("headline") or "").split(" | ")[0].strip()
    ym = (re.search(r"-(20\d\d)(?:-|$)", path)
          or re.search(r"Best of (20\d\d)", article.get("keywords") or "")
          or re.match(r"(20\d\d)", article.get("datePublished") or ""))
    entries = []
    for b in blocks:
        if not isinstance(b, dict) or b.get("@type") != "Audiobook" or not b.get("name"):
            continue
        authors = _names(b.get("author"))
        entries.append({"title": htmlunescape(b["name"]).strip(),
                        "author": authors[0] if authors else None,
                        "narrator": ", ".join(_names(b.get("readBy"))) or None,
                        "url": b.get("mainEntityOfPage")})
    return {"year": int(ym.group(1)) if ym else None, "list": headline or None,
            "entries": entries}


def parse_chart(page: str) -> dict:
    """A bestseller chart page -> {chart, categories: [path], entries: [{rank,
    title, author, narrator, series}]}."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    chart = _text(h1.group(1)) if h1 else None
    categories = sorted(set(re.findall(r'href="(/charts/best/[a-z0-9-]+/\d+)["?#]', page)))
    items = re.split(r'<li class="bc-list-item\s+productListItem"', page)[1:]
    entries = []
    for pos, item in enumerate(items, 1):
        label = re.search(r"aria-label='([^']*)'", item)
        if not label:
            continue

        def field(cls):
            m = re.search(r'<li class="bc-list-item\s+%s"[^>]*>(.*?)</li>' % cls,
                          item, re.S)
            return re.sub(r"^[^:]{2,20}:\s*", "", _text(m.group(1))) if m else None

        authors = field("authorLabel")
        narrators = field("narratorLabel")
        rank = re.search(r">\s*(\d{1,3})\.\s", item)
        entries.append({
            "rank": int(rank.group(1)) if rank else pos,
            "title": htmlunescape(label.group(1)).strip(),
            "author": authors.split(",")[0].strip() if authors else None,
            "narrator": narrators or None,
            "series": field("seriesLabel")})
    return {"chart": chart, "categories": categories, "entries": entries}


# --- fetching -------------------------------------------------------------------

def _page(conn, url: str, fails: list, max_age_days: float | None = None) -> str | None:
    """Mirror-first; a mirrored page older than max_age_days is fetched again."""
    row = conn.execute("SELECT fetched_at FROM raw_pages WHERE url = ?",
                       (url,)).fetchone()
    stale = (row is not None and max_age_days is not None
             and (datetime.now() - datetime.fromisoformat(row[0])).days >= max_age_days)
    raw = _fetch(conn, url, fails, timeout=60, fresh=stale)
    return raw.decode("utf-8", "replace") if raw is not None else None

def _fetched_on(conn, url: str) -> str:
    row = conn.execute("SELECT fetched_at FROM raw_pages WHERE url = ?",
                       (url,)).fetchone()
    return row[0][:10] if row else datetime.now().date().isoformat()


def _add(conn, source: str, kind: str, e: dict, **kw) -> bool:
    key = db.upsert_work(conn, e["title"], e["author"])
    return db.add_accolade(conn, key, source, kind, "listed",
                           narrator=e.get("narrator"), **kw)


def load_audible_best(conn) -> int:
    fails: list = []
    n = parsed = lists_ok = 0
    hub = _page(conn, AUDIBLE_BOTY_HUB, fails, HUB_MAX_AGE_DAYS)
    if hub:
        h = parse_boty_hub(hub)
        parsed += len(h["entries"])
        lists_ok += bool(h["entries"])
        for e in h["entries"]:
            n += _add(conn, "audible-best", "list", e, category=e["category"],
                      year=h["year"], url=AUDIBLE_BOTY_HUB)
        conn.commit()

    articles, page_no, last = {}, 1, 1
    while page_no <= last:
        url = AUDIBLE_BOTY_TAG + ("" if page_no == 1 else f"/page/{page_no}")
        tag = _page(conn, url, fails, HUB_MAX_AGE_DAYS if page_no == 1 else None)
        if tag is None:
            break
        t = parse_boty_tag(tag)
        last = max(last, t["last_page"])
        for path, year in t["articles"]:
            articles.setdefault(path, year)
        page_no += 1

    for path, year in sorted(articles.items(), key=lambda kv: (kv[1], kv[0])):
        url = AUDIBLE + path
        page = _page(conn, url, fails)
        if page is None:
            continue
        lst = parse_blog_list(page, path)
        parsed += len(lst["entries"])
        lists_ok += bool(lst["entries"])
        for e in lst["entries"]:
            n += _add(conn, "audible-best", "list", e,
                      category=lst["list"] or path.rsplit("/", 1)[-1],
                      year=lst["year"] or year, url=e.get("url") or url)
        conn.commit()
    _warn_if_mostly_failing("audible-best", lists_ok, fails)
    db.log_fetch(conn, "audible-best", lists_ok > 0, url=AUDIBLE_BOTY_TAG,
                 n_records=n, n_parsed=parsed,
                 note=_fetch_note(lists_ok, fails, "lists"))
    return n


def load_audible_charts(conn) -> int:
    fails: list = []
    n = parsed = charts_ok = 0
    overall = _page(conn, AUDIBLE_CHART, fails, CHART_MAX_AGE_DAYS)
    urls = [AUDIBLE_CHART]
    if overall:
        cats = parse_chart(overall)["categories"]
        urls += [AUDIBLE + p for p in cats
                 if p.split("/")[3].removesuffix("-audiobooks")
                 in AUDIBLE_CHART_CATEGORIES]
    for url in urls:
        page = overall if url == AUDIBLE_CHART else _page(conn, url, fails,
                                                           CHART_MAX_AGE_DAYS)
        if page is None:
            continue
        c = parse_chart(page)
        parsed += len(c["entries"])
        charts_ok += bool(c["entries"])
        day = _fetched_on(conn, url)
        for e in c["entries"]:
            n += _add(conn, "audible-charts", "popularity", e,
                      category=c["chart"] or "Bestselling Audiobooks",
                      year=int(day[:4]), detail=f"rank {e['rank']} on {day}",
                      url=url)
        conn.commit()
    _warn_if_mostly_failing("audible-charts", charts_ok, fails)
    db.log_fetch(conn, "audible-charts", charts_ok > 0, url=AUDIBLE_CHART,
                 n_records=n, n_parsed=parsed,
                 note=_fetch_note(charts_ok, fails, "charts"))
    return n
