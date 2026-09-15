"""Apple Books top audiobooks — see acclaim_core for the shared machinery."""
from __future__ import annotations

import email.utils
import json
import re

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Apple Books top audiobooks -------------------------------------------------

# Apple's marketing RSS generator publishes the US "Top Audiobooks" chart as
# JSON; 100 is the largest size it serves, and robots.txt allows it. Each result
# carries the book name, artistName (the author, co-authors joined with "&"),
# genres and releaseDate — no narrator. feed.updated dates the snapshot.
#
# Names carry storefront decoration: "(Unabridged)", series parentheticals
# ("Red Rising (Red Rising)"), "[Dramatized Adaptation]", and series volumes as
# a subtitle ("Carl's Doomsday Scenario: Dungeon Crawler Carl, Book 2").
# Results in the "Kids & Young Adults" genre are skipped.
APPLE_AUDIO_URL = ("https://rss.marketingtools.apple.com/api/v2/us/audio-books/"
                   "top/100/audio-books.json")
APPLE_AUDIO_CHART = "Top Audiobooks"
APPLE_KIDS_GENRE = "Kids & Young Adults"
_AP_BRACKET_RE = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])")
_AP_SERIES_RE = re.compile(r"\s*:\s*[^:]*,\s*Book\s+\d+\s*$", re.I)
_AP_TAG_RE = re.compile(r"\s*:\s*(?:A\s+)?(?:GMA|Reese's|Oprah's)[^:]*?"
                        r"Book Club(?:\s+Pick)?(?=\s*(?::|$))", re.I)
_AP_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:&|,|\band\b)\s*")
_AP_SUFFIX_RE = re.compile(r"^(?:jr|sr|ii|iii|iv|md|m\.d|phd|ph\.d)\.?$", re.I)


def clean_apple_title(name: str) -> str:
    """'Carl's Doomsday Scenario: Dungeon Crawler Carl, Book 2 (Unabridged)'
    -> "Carl's Doomsday Scenario"."""
    t = _AP_BRACKET_RE.sub("", name or "").strip(" :")
    t = _AP_TAG_RE.sub("", t)
    t = _AP_SERIES_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" :")


def first_author(artist: str) -> str | None:
    """The first credited author: 'James Patterson & James O. Born' ->
    'James Patterson'; 'Bessel van der Kolk, M.D.' -> 'Bessel van der Kolk'."""
    names = [p for p in _AP_AUTHOR_SPLIT_RE.split(artist or "")
             if p and not _AP_SUFFIX_RE.match(p.strip())]
    return names[0].strip() if names else None


def parse_apple_audio(payload: str | bytes) -> tuple[str | None, list[dict]]:
    """-> (snapshot date YYYY-MM-DD, [{rank, title, name, author, genres,
    release_date, url}]) with the kids' chart entries dropped; rank is the
    position in the full chart."""
    feed = json.loads(payload).get("feed", {})
    snap = None
    if feed.get("updated"):
        try:
            snap = email.utils.parsedate_to_datetime(feed["updated"]).date().isoformat()
        except (TypeError, ValueError):
            snap = None
    out = []
    for rank, r in enumerate(feed.get("results", []), 1):
        genres = [g.get("name") for g in r.get("genres", []) if g.get("name")]
        if APPLE_KIDS_GENRE in genres:
            continue
        out.append({"rank": rank, "title": clean_apple_title(r.get("name")),
                    "name": r.get("name"), "author": first_author(r.get("artistName")),
                    "genres": [g for g in genres if g != "Audiobooks"],
                    "release_date": r.get("releaseDate"), "url": r.get("url")})
    return snap, out


def load_apple_audio(conn) -> int:
    import datetime
    fails: list = []
    raw = _fetch(conn, APPLE_AUDIO_URL, fails, accept="application/json",
                 timeout=60, fresh=True)
    snap, entries = parse_apple_audio(raw) if raw else (None, [])
    snap = snap or datetime.date.today().isoformat()
    year = int(snap[:4])
    n = 0
    for e in entries:
        if not e["title"]:
            continue
        key = db.upsert_work(conn, e["title"], e["author"])
        if db.add_accolade(conn, key, "apple-audio", "popularity", "listed",
                           category=APPLE_AUDIO_CHART, year=year,
                           detail=f"rank {e['rank']} on {snap}", url=e["url"]):
            n += 1
    conn.commit()
    db.log_fetch(conn, "apple-audio", bool(entries), url=APPLE_AUDIO_URL,
                 n_records=n, n_parsed=len(entries),
                 note=_fetch_note(len(entries), fails, "chart entries"))
    return n
