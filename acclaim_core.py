#!/usr/bin/env python3
"""Shared machinery for the acclaim sources: transports, text repair, guards.

See acclaim.py for the CLI and the source registry; sources/ for the adapters.

ORIGINAL MODULE DOC follows, because the design notes belong with the code
they constrain:

Awards, best-of lists and canon — a sourced corpus, joined to the shelf.

The want-list answers "is it on a shelf this morning"; `hotlist.py` answers
"where will I be in the queue". This answers the question before both of them:
*what is worth reading*, from the bodies that decide it — and then hands the
answer to the library machinery to find a copy.

Design notes that are load-bearing:

  * **Original sources, Wikipedia only as fallback.** Measured 2026-09-06:
    Wikidata carries 818 Booker nominees but only 21 Pulitzer finalists (there
    are ~200), 6 Women's Prize nominees and 1 for the NBCC. Fine for winners,
    useless for the shortlists — which is most of what makes a corpus worth
    having. Anything sourced only from Wikidata is flagged as such.
  * **Three transports.** Most sites yield to plain HTTP. A few defeat scripted
    clients entirely — pulitzer.org 403s `urllib` *and* `curl`, which is TLS
    fingerprinting, not headers — and those are fetched by JavaScript running
    inside a real Chrome tab, which POSTs its harvest to `tools/collector.py`.
  * **Raw first.** Scripted fetches mirror into `raw_pages` before parsing, and
    browser harvests land as JSON in `harvest/`. A parser fix must never cost a
    re-crawl; for the browser tier, where re-crawling means driving Chrome by
    hand, that matters far more than usual.

    uv run acclaim.py pull --source pulitzer      # one source
    uv run acclaim.py pull --all                  # every scriptable source
    uv run acclaim.py browser-plan                # what needs a Chrome pass
    uv run acclaim.py stats                       # coverage + provenance
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


# --- fetching -------------------------------------------------------------------

def _fetch(conn, url: str, fails: list, *, accept: str = "text/html",
           timeout: float = 60) -> bytes | None:
    """Mirror-first fetch. Returns the body, or None with the reason recorded.

    Every loader here used to `except Exception: continue`, and that is exactly
    how a Booker backfill quietly fetched 5 pages out of 733: a concurrent run
    held the SQLite write lock, `_get` archives *inside* its own try, and
    'database is locked' came back through the same `except Exception` as a
    404 would. 728 silent failures were indistinguishable from 728 books with
    no prize history.

    So: failures are counted and surfaced in the fetch log, never swallowed.
    A source that suddenly returns nothing must look different from a source
    that has nothing to return.
    """
    raw = db.get_raw_page(conn, url)
    if raw is not None:
        return raw
    import bayarea_lookup as B
    # Arm the mirror here rather than in each loader. Three sources had already
    # been written without calling set_archive, and the only symptom was zero
    # rows in raw_pages — the pull still "worked", so the raw-first guarantee
    # was quietly not holding for them.
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


# A source that suddenly parses far less than it used to has almost certainly
# broken, not shrunk. Five separate bugs this repo has already hit looked
# identical from the outside — the pull reported success and simply returned
# less:
#   Booker deadlock          5 pages of 733
#   FT {{blue ribbon}} case  7 winners of 21
#   sfadb quoted titles      a third of the short fiction it should have
#   ISFDB author window      0 containers, 0 errors
#   Douban CJK work_key      39 accolades from 540 harvested books
# Only the last was caught, and only because a human read the number. This
# makes that comparison the machine's job.
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

# Some sources serve double-encoded UTF-8: a curly apostrophe (U+2019, bytes
# e2 80 99) comes back as the three characters those bytes name in Latin-1.
# PEN does this on 5 of its 999 rows, which is exactly the frequency that
# survives a spot-check and then quietly poisons a work_key.
#
# Round-tripping through Latin-1 undoes it. The guard is that the round trip
# must SUCCEED and yield different text: genuine Latin-1 text like 'château'
# fails to decode as UTF-8 and is returned untouched.
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
    """UTF-8 where possible, Latin-1 where not.

    sfadb serves Latin-1: decoding it as UTF-8 turned 'P. Djèlí Clark' into
    'P. Dj\ufffdl\ufffd Clark', which then became the author's name in the corpus.
    """
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
