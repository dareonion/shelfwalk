#!/usr/bin/env python3
"""Shared machinery for the acclaim sources: transports, the raw mirror, text
repair and the yield guard. acclaim.py holds the CLI and the source registry;
sources/ holds one adapter per awarding body.

Design rules:

  * **Original sources; Wikipedia/Wikidata only as a flagged fallback.**
    Wikidata is complete for winners but thin on shortlists (21 of ~200
    Pulitzer finalists, 2026-09).
  * **Three transports.** Plain HTTP where a site allows it; JavaScript in a
    real Chrome tab where it doesn't (pulitzer.org fingerprints TLS, so no
    header helps), POSTing the harvest to `tools/collector.py`; Wikipedia as
    the fallback.
  * **Raw first.** HTTP responses mirror into `raw_pages` and browser harvests
    land in `harvest/`, so a parser fix never needs a re-crawl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from html import unescape as htmlunescape
from typing import Callable

import catalog_db as db

HARVEST_DIR = "harvest"

# Transports, in order of preference.
HTTP = "http"            # scriptable; mirrored through bayarea_lookup._get
BROWSER = "browser"      # needs a Chrome pass (bot-walled or paywalled)
WIKIDATA = "wikidata"    # fallback and cross-check only


@dataclass
class Source:
    """One place accolades come from.

    Adding a source should mean writing one parser and registering it. If an
    adapter needs more than that, the framework is wrong, not the source.
    """
    key: str
    label: str
    kind: str                       # 'award' | 'list' | 'popularity'
    transport: str
    load: Callable                  # (conn) -> int records written
    cadence: str = "annual"
    note: str = ""
    forms: tuple = field(default_factory=lambda: ("book",))
    harvest: bool = False           # prefers a browser harvest over its transport


# --- fetching -------------------------------------------------------------------

def _fetch(conn, url: str, fails: list, *, accept: str = "text/html",
           timeout: float = 60, fresh: bool = False) -> bytes | None:
    """Mirror-first fetch. Returns the body, or None with the reason appended
    to `fails` — never swallowed, so a source whose fetches fail (a locked
    database included) looks different from one with nothing to return.

    fresh=True always fetches live (still mirrored), for pages that change
    under the same URL such as bestseller charts; a failed fresh fetch returns
    None rather than an old copy under today's date.
    """
    if not fresh:
        raw = db.get_raw_page(conn, url)
        if raw is not None:
            return raw
    import bayarea_lookup as B
    # armed here, not in each loader, so no loader can skip the mirror
    _arm_archive(conn)
    try:
        return B._get(url, accept=accept, timeout=timeout)
    except Exception as exc:                             # noqa: BLE001
        fails.append(f"{url}: {type(exc).__name__}: {exc}")
        return None


def _arm_archive(conn) -> None:
    import bayarea_lookup as B
    if getattr(B, "_archive_path", None):
        return
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if path:
        B.set_archive(path)


def _fetch_note(ok_count: int, fails: list, what: str) -> str:
    """One line summarising a pull, including how it failed if it did."""
    note = f"{ok_count} {what}"
    if fails:
        note += f"; {len(fails)} fetch failures, first: {fails[0][:120]}"
    return note


# A source that parses far less than its best previous run has broken, not
# shrunk: a parser break reports success and simply returns less.
YIELD_DROP_THRESHOLD = 0.6      # of the best previous run
YIELD_MIN_HISTORY = 1           # runs needed before the check has an opinion


def check_yield(conn, source: str, n_parsed: int | None) -> str | None:
    """Compare this run's parsed count against the source's own history.

    Returns a warning line, or None when the yield looks sane. Compares
    against the BEST previous run, not the last: two bad runs in a row must
    not quietly become the new normal.
    """
    if n_parsed is None:
        return None
    history = db.past_yields(conn, source)
    if len(history) < YIELD_MIN_HISTORY:
        return None
    best = max(history)
    if best <= 0:
        return None
    if n_parsed >= best * YIELD_DROP_THRESHOLD:
        return None
    return (f"{source}: parsed {n_parsed}, but a previous run parsed {best} "
            f"({n_parsed / best:.0%} of it). Treat this as a parser break "
            f"until shown otherwise.")


def _warn_if_mostly_failing(source: str, ok_count: int, fails: list) -> None:
    if fails and ok_count == 0:
        print(f"  ! {source}: every fetch failed ({len(fails)}). "
              f"First: {fails[0][:160]}", file=sys.stderr)
    elif len(fails) > ok_count:
        print(f"  ! {source}: {len(fails)} failures vs {ok_count} successes — "
              f"first: {fails[0][:160]}", file=sys.stderr)

# --- text repair ----------------------------------------------------------------

# Some sources (a few PEN rows) serve double-encoded UTF-8: a curly apostrophe
# (U+2019, bytes e2 80 99) arrives as the three Latin-1 characters those bytes
# name. A Latin-1 round trip undoes it. The round trip must succeed and change
# the text, so genuine Latin-1 like 'château' fails to decode and is returned
# untouched.
_MOJI_LEAD = re.compile("[ÂÃâã]")


def demojibake(s: str) -> str:
    if not s or not _MOJI_LEAD.search(s):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    # a repair that introduces control characters is not a repair
    if fixed == s or any(ord(c) < 32 and c not in "\t\n" for c in fixed):
        return s
    return fixed


def _decode_page(raw: bytes) -> str:
    """UTF-8 where possible, Latin-1 where not — sfadb serves Latin-1, and
    decoding it as UTF-8 mangles names like 'P. Djèlí Clark'."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")

# --- credit parsing -------------------------------------------------------------

# 'Angel Down, by Daniel Kraus (Atria Books)' and its many near-misses. The
# publisher is optional (Drama entries carry none), and titles routinely
# contain commas — 'The Netanyahus: An Account of a Minor and Ultimately Even
# Negligible Episode…, by Joshua Cohen' — so the author split anchors on the
# LAST ', by ', not the first.
_BY_RE = re.compile(r",\s+by\s+", re.I)
_PUB_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def parse_credit(raw: str) -> dict:
    """'<Title>, by <Author> (<Publisher>)' -> dict, tolerantly.

    Returns title/author/publisher with author and publisher possibly None.
    'No award' and similar non-entries return an empty title, which callers
    skip — a year with no prize is data, not a parse failure.
    """
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s or re.fullmatch(r"no award(ed)?\.?", s, re.I):
        return {"title": "", "author": None, "publisher": None}

    publisher = None
    m = _PUB_RE.search(s)
    if m:
        # only strip a trailing paren group if it looks like a publisher rather
        # than part of the title ('(Deluxe Limited Edition)' stays)
        inner = m.group(1).strip()
        if inner and not re.search(r"\b(edition|vol\.?|volume|reprint)\b", inner, re.I):
            publisher = inner
            s = s[:m.start()].strip()

    parts = _BY_RE.split(s)
    if len(parts) >= 2:
        title = _BY_RE.split(s)[0] if len(parts) == 2 else ", by ".join(parts[:-1])
        author = parts[-1].strip()
    else:
        title, author = s, None
    return {"title": title.strip().strip(","),
            "author": (author or None), "publisher": publisher}


def _kp_title_case(s: str) -> str:
    """Kirkus shouts its titles and bylines; only fold the all-caps ones."""
    s = _strip(htmlunescape(s))
    if not s or s != s.upper():
        return s
    # str.title() capitalises after an apostrophe — "Margo'S Got Money Troubles"
    return re.sub(r"[A-Za-z']+", lambda m: m.group(0).capitalize()
                  if "'" not in m.group(0)
                  else m.group(0)[0].upper() + m.group(0)[1:].lower(), s.lower())


def _wiki_plain(s: str) -> str:
    """[[Target|Display]] / [[Name]] -> the displayed text; drop templates."""
    s = re.sub(r"\{\{[^{}]*\}\}", " ", s or "")
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", " ", s, flags=re.S)
    s = re.sub(r"'{2,}", "", s)
    return re.sub(r"\s+", " ", s).strip(" ,")


def _flat_name(s: str) -> str:
    """Punctuation-free lowercase, for loose title/name comparison."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()
