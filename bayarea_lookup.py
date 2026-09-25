#!/usr/bin/env python3
"""Look up the shelfwalk want-list at Bay Area library systems.

Checks every title in `shelfwalk.db` (Peoria-seeded rows plus the
`wantlist_*.json` entries merged in on each run) for whether, and where, it is
on the shelf at:

  sccl      Santa Clara County Library District  (BiblioCommons gateway JSON API)
  sjpl      San José Public Library               (BiblioCommons gateway JSON API)
  linkplus  LINK+ union catalog                   (INN-Reach WebPAC, HTML)
  mvpl      Mountain View Public Library          (classic Innovative WebPAC, HTML)
            — only when named; see SKIPPED_SYSTEMS

Plain HTTP, no browser. Results land in `remote_bibs`, `remote_editions` and
`remote_availability`, and the Bay Area markdown is regenerated after every run.

    uv run bayarea_lookup.py                          # every title, default systems in parallel
    uv run bayarea_lookup.py --system sccl --limit 5  # quick spot check
    uv run bayarea_lookup.py --resume                 # only titles not yet looked up
    uv run bayarea_lookup.py --rediscover             # force new edition searches
    uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe, prints only
    uv run bayarea_lookup.py --enrich                 # just the record-detail pass

Routine SCCL/SJPL/LINK+ runs reuse discovery for seven days and poll each
distinct physical bib once per run. New or changed wants search immediately.
--rediscover bypasses the discovery cache; availability is never reused across runs.

Matching is fuzzy: search title + author surname, then score candidates by
normalized title similarity (pinyin titles meet the catalogs' romanized
fields). A title with no match is recorded as bib_id NULL: that library doesn't
hold it.

The best match anchors the title, and every other version of the same work in
the results rides along in `remote_editions`: other physical formats and
printings, audiobooks (physical and digital), eBooks, and Chinese
editions. Movies and music are never candidates; digital
editions are linked but carry no shelf state (a license queue isn't a shelf).
"""
from __future__ import annotations

import argparse
import difflib
import glob
import gzip
import html as htmllib
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import catalog_db as db
import report

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
GATEWAY = "https://gateway.bibliocommons.com/v2/libraries"

# BiblioCommons formats we'll accept as "this book". BOOK_PCD / BOOK_CD / KIT
# cover the bilingual book-plus-audio kits on the list; audiobooks are physical
# audio; EBOOK/EAUDIOBOOK are tracked as digital editions (movies and music
# stay out).
BC_BOOK_FORMATS = {"BK", "BOARD_BK", "PICTURE_BOOK", "PAPERBACK", "LARGE_PRINT",
                   "BOOK_PCD", "BOOK_CD", "KIT",
                   "AB", "AUDIOBOOK_CD", "PLAYAWAY_AUDIOBOOK",
                   "EBOOK", "EAUDIOBOOK"}
_BC_AUDIO_FORMATS = {"AB", "AUDIOBOOK_CD", "PLAYAWAY_AUDIOBOOK",
                     "BOOK_CD", "BOOK_PCD", "SPOKEN_CD", "BOOK_PAUDIO"}
_BC_PRINT_FORMATS = {"BK", "PAPERBACK", "LARGE_PRINT", "LPRINT", "KIT",
                     "GRAPHIC_NOVEL"}
# Known codes that are not books. These and any unseen code class as "other",
# so a Blu-ray never counts as a shelf copy or takes a hot-list hold. BR sits
# beside BOARD_BK for the same picture-book titles ('Brown Bear, Brown Bear'),
# so it is read as a braille printing (inferred, not documented). test_mirror.py
# fails on any mirrored code missing from these sets.
_BC_NONBOOK_FORMATS = {"DVD", "BLURAY", "VIDEO_ONLINE", "VIDEO_DOWNLOAD",
                       "NONSTANDARD_VIDEO", "PRELOADED_VIDEO_PLAYER", "MUSIC_CD",
                       "MUSIC_DOWNLOAD", "MN", "MAP", "UK", "BR",
                       "BOOK_CLUB_KIT", "RESTRICTED_BOOK_MP3"}
# Digital editions are listed and linked but have no shelf: their availability
# is a licensing queue (Libby/hoopla), not a branch, so no state is recorded.
DIGITAL_CLASSES = ("ebook", "eaudio")

# Language editions we surface alongside the main match (in addition to
# other physical formats of the same work).
EXTRA_LANGS = ("chi",)
EXCLUDED_LANGS = ("spa", "jpn", "fre")
# Versions tracked per (title, system) — bounds the per-title availability
# fetches when a classic is printed in a dozen editions.
MAX_EDITIONS = 8
# Extra same-language versions must be near-exact: true editions of the same
# work sit at 0.98+ (subtitle variants ride the stem rule), while short-suffix
# spinoffs crowd the band just below — 'The Very Hungry Caterpillar's Eid'
# 0.900, '…Drive the Sleigh!' 0.901, 'Chicka Chicka I Love Mom' 0.917,
# 'Dragons Love Tacos 2' 0.947. A borderline true translation ('Ye shou guo',
# 'Pete el gato') re-enters through the translation rule, correctly labeled.
EDITION_MIN_RATIO = 0.95

# Below ~0.75 nearly everything is a lookalike (shared series prefixes, 'my
# first X' phrasing); the only legitimate sub-0.75 matches were exact titles
# dragged down by the author penalty, so that penalty is mild (0.85).
MATCH_THRESHOLD = 0.75
AUTHOR_MISMATCH_PENALTY = 0.85


# --- HTTP -----------------------------------------------------------------------

# Every response body is mirrored verbatim into raw_pages, so a parser or
# matcher fix re-reads what was already fetched. set_archive() points the
# mirror at the run's DB; _get() feeds it.
_archive_path = None
_archive_local = threading.local()


def set_archive(db_path: str) -> None:
    global _archive_path
    _archive_path = db_path


def _archive(url: str, body: bytes) -> None:
    if not _archive_path:
        return
    conn = getattr(_archive_local, "conn", None)
    if conn is None or getattr(_archive_local, "path", None) != _archive_path:
        conn = db.open_db(_archive_path)
        _archive_local.conn, _archive_local.path = conn, _archive_path
    with conn:
        db.store_raw_page(conn, url, body,
                          datetime.now().isoformat(timespec="seconds"))


# Minimum spacing between requests to the SAME host, across threads: sccl and
# sjpl share gateway.bibliocommons.com, which 403s uncoordinated threads.
_HOST_SPACING = 0.4
# Hosts that ask for more: robots.txt crawl delays, and slower spacing for
# sites fetched in bulk only by the acclaim sources.
HOST_SPACING = {"www.ala.org": 10.0, "www.rusaupdate.org": 10.0,
                "www.kirkusreviews.com": 10.0, "www.audible.com": 2.0}
# Cap a single back-off: giving up on one title beats escalating waits that
# stall the whole run into the service timeout.
THROTTLE_MAX_WAIT = 30
_host_gate = threading.Lock()
_host_last: dict = {}


def _pace(url: str) -> None:
    host = urllib.parse.urlsplit(url).netloc
    while True:
        with _host_gate:
            now = time.monotonic()
            wait = (_host_last.get(host, 0.0)
                    + HOST_SPACING.get(host, _HOST_SPACING) - now)
            if wait <= 0:
                _host_last[host] = now
                return
        time.sleep(wait)


def _get(url: str, accept: str = "application/json", tries: int = 3,
         timeout: float = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": accept})
    last_err = None
    for attempt in range(tries):
        _pace(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                _archive(url, data)
                return data
        except Exception as e:  # URLError, HTTPError, timeout
            last_err = e
            # an HTTPError *is* the response: unclosed, its socket lingers in
            # CLOSE-WAIT for the rest of the run
            try:
                e.close()
            except Exception:
                pass
            if getattr(e, "code", None) in (404, 410):  # gone stays gone: no retry
                break
            if getattr(e, "code", None) in (403, 429):  # throttled: back off
                wait = min(20 * (attempt + 1), THROTTLE_MAX_WAIT)
                # say so: a silent multi-minute sleep looks like a hang, and a
                # per-title log line only prints once every fetch is done
                print(f"    throttled ({e.code}) — waiting {wait}s: "
                      f"{urllib.parse.urlsplit(url).netloc}", flush=True)
                time.sleep(wait)
            else:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {attempt + 1} tries: {url}: {last_err}")


# --- query building / fuzzy matching --------------------------------------------

def query_terms(title: str, author: str = None) -> tuple[str, str]:
    """(cleaned title, author surname) to feed a catalog search box.

    Drops the parenthetical glosses our list uses ('(Animal Band)', '(bilingual
    EN/ZH + audio)') and parallel titles after '=' or '/'.
    """
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = re.split(r"[=/]", t)[0]
    t = re.sub(r"\s+", " ", t).strip(" .,:;")
    surname = ""
    if author:
        surname = author.split(",")[0].strip()
        surname = re.sub(r"[^A-Za-z' -]", "", surname).strip()
    return t, surname


_CJK = re.compile(r"[一-鿿]")


def _norm(s: str) -> str:
    """Fold case/diacritics/punctuation so 'Hervé' == 'herve', keep CJK."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9一-鿿]+", " ", s.lower())
    return s.strip()


# pypinyin's colloquial readings vs the ALA-LC romanization catalogs actually use
_ALA_LC = {"shei": "shui"}


def _pinyin(s: str) -> str:
    """CJK → space-separated pinyin ('好餓的毛毛蟲' → 'hao e de mao mao chong').

    This is how the CJK want-list meets these catalogs' romanized title fields,
    and it also bridges traditional/simplified variants (both → the same pinyin).
    """
    from pypinyin import lazy_pinyin
    return " ".join(_ALA_LC.get(p, p) for p in lazy_pinyin(_norm(s)))


# A pinyin-mediated comparison must clear a higher bar: short syllable streams
# ('zhe shi wo de' vs 'zhe bu shi wo de mao zi') look far more alike to a
# sequence matcher than distinct English titles do.
PINYIN_MIN_RATIO = 0.85

_PINYIN_SYL = re.compile(r"^(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])?[aeiouv]{1,3}(n|ng|r)?$")


def _looks_pinyin(norm_text: str) -> bool:
    """True for romanized-Chinese strings ('xiao xiong de wei ba') — the DB's
    Peoria want-list stores pinyin natively, so it never carries a CJK flag.
    Majority vote, not all(): joined syllables ('Keke', 'Suqi') are common in
    catalog romanization and shouldn't unmask the string as non-pinyin."""
    toks = norm_text.split()
    if len(toks) < 3:
        return False
    hits = sum(1 for t in toks if _PINYIN_SYL.match(t))
    return hits * 3 >= len(toks) * 2


def _forms(title: str) -> list[tuple[str, bool, bool, bool]]:
    """[(text, is_stem, has_subtitle, is_pinyin)] — title, stem, pinyin forms.

    A stem form only exists when what it drops is a parallel title ('= Tren
    de carga', '/ …') or a *descriptive* subtitle ('a lift-the-flap book') —
    dropping a volume-naming subtitle would turn 'Grumpy monkey : mom for a
    day' into every other Grumpy Monkey.
    """
    m = re.search(r"[:=/：]", title or "")
    stem = (title[:m.start()] if m else title).strip()
    has_sub = bool(stem) and stem != (title or "").strip()
    out = [(title, False, has_sub, False)]
    if has_sub and (m.group(0) in "=/"
                    or _DESCRIPTIVE_SUB.search(title[m.end():])):
        out.append((stem, True, has_sub, False))
    for text, is_stem, hs, _ in list(out):
        if _CJK.search(text):
            out.append((_pinyin(text), is_stem, hs, True))
    return out


def title_score(want_title: str, cand_titles) -> float:
    """Best normalized-similarity between any want form and any candidate form.

    Each side also offers its pre-subtitle stem ('Dear zoo : a lift-the-flap
    book' → 'Dear zoo'), but a stem may only pair with a string that has no
    subtitle of its own, and stem pairs are slightly dampened — otherwise every
    'Grumpy monkey : <adventure>' would tie at 1.0 with every other one.
    Pairs where either side went through pinyin only count above PINYIN_MIN_RATIO.
    """
    best = 0.0
    for w, w_stem, w_sub, w_pin in _forms(want_title):
        wn = _norm(w)
        if not wn:
            continue
        for c in cand_titles:
            for cv, c_stem, c_sub, c_pin in _forms(c or ""):
                if (w_stem and c_sub) or (c_stem and w_sub):
                    continue  # dropping a subtitle may not erase a mismatch
                cn = _norm(cv)
                if not cn:
                    continue
                r = difflib.SequenceMatcher(None, wn, cn).ratio()
                # Chinese comparisons — pinyin or CJK — must be near-exact:
                # short syllable/character strings blur ('zhe shi wo de' vs
                # 'zhe bu shi wo de mao zi' is That's Not My Hat, and one 不
                # flips the meaning)
                loose = (w_pin or c_pin
                         or (_looks_pinyin(wn) and _looks_pinyin(cn))
                         or (_CJK.search(wn) and _CJK.search(cn)))
                if loose and r < PINYIN_MIN_RATIO:
                    continue
                if w_stem or c_stem:
                    # a pair that dropped a subtitle must be near-exact on what
                    # remains — 'Chicka Chicka I love you' vs the stem of
                    # 'Chicka chicka you you : a mirror book' scores 0.84 on
                    # shared prefix alone, and that's a different book
                    if r < 0.9:
                        continue
                    r *= 0.98  # an exact full-title match should win ties
                best = max(best, r)
    return best


def _fmt_bonus(our_format: str, cand_class: str) -> float:
    """Nudge toward the same shelf format (board vs picture) when we know ours."""
    if our_format == "board" and cand_class == "board":
        return 0.08
    if our_format in ("picture", "reader") and cand_class in ("picture", "book"):
        return 0.04
    return 0.0


def _author_matches(surname: str, cand) -> bool:
    return bool(surname) and bool(cand.get("authors")) and \
        _norm(surname) in _norm(" ".join(cand["authors"]))


# Subtitles that describe the printing, not a different work — these may ride
# the stem rule ('Dear zoo : a lift-the-flap book' is still Dear Zoo).
_DESCRIPTIVE_SUB = re.compile(
    r"^\s*(a|an|the)\b|^\s*\[|\bbook\b|\bstory\b|\bstories\b|\btale\b|"
    r"\banniversary\b|\bedition\b", re.I)


def _display_title(cand) -> str:
    """What we store/print: BiblioCommons splits 'Grumpy Monkey : Too Many
    Bugs' into title + subtitle, and the bare half misidentifies the record."""
    sub = (cand.get("subtitle") or "").strip()
    t = cand.get("title") or ""
    return f"{t} : {sub}" if sub else t


def _references_other_work(want_title: str, candidate_title: str) -> bool:
    """A new title 'from/with <wanted book>' names a spin-off, not an edition."""
    match = re.search(r"\S.+?\s+(?:from|with)\s+(.+)$", candidate_title or "", re.I)
    return bool(match and title_score(want_title, [match.group(1)]) >= EDITION_MIN_RATIO
                and _norm(want_title) != _norm(candidate_title))


def _cand_score(want_title: str, surname: str, cand) -> float:
    """Title similarity for one candidate, with the author-mismatch damp.

    A candidate's subtitle is part of its identity — the bare title never
    scores alone ('Grumpy Monkey' + subtitle 'Too Many Bugs' is a series
    volume, not the picture book). A descriptive subtitle joins with ':' so
    the stem rule still recognizes the same work; a volume-naming one is
    fused in, leaving only the full title to match.
    """
    if _references_other_work(want_title, _display_title(cand)):
        return 0.0
    sub = (cand.get("subtitle") or "").strip()
    if sub:
        joiner = " : " if (_DESCRIPTIVE_SUB.search(sub)
                           or _norm(sub) in _norm(want_title)) else " "
        names = [f"{cand.get('title') or ''}{joiner}{sub}"]
    else:
        names = [cand.get("title") or ""]
    if cand.get("alt_title"):
        names.append(cand["alt_title"])
    score = title_score(want_title, names)
    if surname and cand.get("authors") and not _author_matches(surname, cand):
        # penalize, don't reject: 'Ten apples up on top!' is cataloged
        # under LeSieg, not Seuss
        score *= AUTHOR_MISMATCH_PENALTY
    return score


def pick_best(row_title: str, row_author: str, row_format: str, candidates,
              lang: str = None, enforce_lang: bool = True):
    """candidates: dicts with title/subtitle/authors/format_class. → (cand, score).

    A want with `lang` set (e.g. 'chi') only accepts candidates in that
    language, and at a stricter threshold — otherwise 'Cher zoo' happily takes
    the English Dear Zoo, and 'T'choupi va sur le pot' any other T'choupi.
    enforce_lang=False keeps just the stricter threshold, for catalogs whose
    search results don't say what language a record is in (LINK+).
    """
    if lang in EXCLUDED_LANGS:
        return None, 0.0
    candidates = [c for c in candidates if c.get("language") not in EXCLUDED_LANGS]
    if lang and enforce_lang:
        candidates = [c for c in candidates if c.get("language") == lang]
    threshold = 0.8 if lang else MATCH_THRESHOLD
    want_title, _ = query_terms(row_title)
    surname = query_terms("", row_author)[1] if row_author else ""
    best, best_key, best_score = None, None, 0.0
    for i, cand in enumerate(candidates):
        score = _cand_score(want_title, surname, cand)
        score += _fmt_bonus(row_format or "", cand.get("format_class") or "")
        # Near-equal scores: prefer a physical edition (the primary anchors the
        # shelf matrix; digital rides along as an extra), then the edition with
        # more copies on the shelf (WebPAC candidates carry their items), then
        # catalog relevance order.
        items = cand.get("items") or []
        n_avail = sum(1 for it in items if it.get("state") == "available")
        key = (round(score, 2),
               (cand.get("format_class") or "") not in DIGITAL_CLASSES,
               n_avail, len(items), -i)
        if best_key is None or key > best_key:
            best, best_key, best_score = cand, key, score
    if best_score >= threshold:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


def pick_all(row_title: str, row_author: str, row_format: str, candidates,
             lang: str = None, enforce_lang: bool = True,
             max_editions: int = MAX_EDITIONS):
    """(primary, score, editions) — the best match plus every other version of
    the same work worth showing: other physical formats/printings that clear
    the normal title bar ('edition'), translations in EXTRA_LANGS
    ('translation' — a translated title can't fuzzy-match the original, so
    same-author + language stands in), and physical audiobooks ('audio' —
    compilations like 'Brown bear & friends' carry the story retitled).

    The loose rules only trust candidates from an AND-semantics search
    (cand['strict']: WebPAC keyword results, BC's fielded search — where a
    translation surfaces via its 'Translation of:' note / uniform title), so a
    fuzzy smart-search result can't drift to the author's *other* works. Even
    then, spinoff translations can sneak in ('Translation of: The Very Hungry
    Caterpillar's Easter Colors' contains the original title's every word), so
    only one translation per (language, format) is kept — the one whose title
    length sits closest to the want's, and spinoff titles run long.
    Editions include the primary (kind='primary').
    """
    candidates = [c for c in candidates if c.get("language") not in EXCLUDED_LANGS]
    primary, score = pick_best(row_title, row_author, row_format, candidates,
                               lang, enforce_lang)
    if primary is None:
        return None, score, []
    want_title, _ = query_terms(row_title)
    surname = query_terms("", row_author)[1] if row_author else ""
    editions = [dict(primary, kind="primary", match_score=score)]
    seen = {primary.get("bib_id")}
    plang = primary.get("language")
    trans = {}  # (language, format_class) -> (title_len_diff, cand, score)
    for cand in candidates:
        bid = cand.get("bib_id")
        if not bid or bid in seen:
            continue
        clang = cand.get("language")
        # editions must share the primary's language (or the pinned one) —
        # a foreign-language record is a translation and takes the verified
        # translation route, labeled, or not at all
        lang_ok = (clang in ((lang, None) if lang else (plang, None))
                   or not enforce_lang)
        s = round(_cand_score(want_title, surname, cand), 3)
        if s >= EDITION_MIN_RATIO and lang_ok:
            seen.add(bid)
            editions.append(dict(cand, kind="edition", match_score=s))
        elif cand.get("strict") and \
                cand.get("format_class") in ("audio", "eaudio") and \
                _author_matches(surname, cand) and \
                (lang is None or clang in (lang, None)):
            seen.add(bid)
            editions.append(dict(cand, kind="audio", match_score=s))
        elif cand.get("strict") and lang is None and clang in EXTRA_LANGS and \
                _author_matches(surname, cand):
            key = (clang, cand.get("format_class"))
            diff = abs(len(_norm(cand.get("title") or "")) - len(_norm(want_title)))
            if key not in trans or diff < trans[key][0]:
                trans[key] = (diff, cand, s)
    for diff, cand, s in trans.values():
        if cand["bib_id"] not in seen:
            seen.add(cand["bib_id"])
            editions.append(dict(cand, kind="translation", match_score=s))
    if len(editions) > max_editions:
        # translations and audiobooks are the rare finds; the Nth same-language
        # printing is what gets cut
        rest = sorted(editions[1:], key=lambda e:
                      {"translation": 0, "audio": 1, "edition": 2}[e["kind"]])
        editions = editions[:1] + rest[:max_editions - 1]
    return dict(editions[0]), score, editions


# --- BiblioCommons (SCCLD, SJPL) ------------------------------------------------

def _bc_format_class(fmt: str) -> str:
    if fmt == "BOARD_BK":
        return "board"
    if fmt == "PICTURE_BOOK":
        return "picture"
    if fmt in _BC_AUDIO_FORMATS:
        return "audio"
    if fmt in ("EBOOK", "GRAPHIC_NOVEL_DOWNLOAD"):
        return "ebook"
    if fmt == "EAUDIOBOOK":
        return "eaudio"
    if fmt in _BC_PRINT_FORMATS:
        return "book"
    return "other"          # _BC_NONBOOK_FORMATS, and any code not seen yet


def bc_parse_search(payload: dict, strict: bool = False) -> list[dict]:
    """Gateway search JSON → candidate list in result order (book formats only).

    strict marks candidates from an AND-semantics query (the fielded search) —
    the only ones pick_all's author-anchored rules are allowed to trust.
    """
    bibs = payload.get("entities", {}).get("bibs", {})
    seen, out = set(), []
    for res in payload.get("catalogSearch", {}).get("results", []):
        ids = res.get("manifestations") or [res.get("representative")]
        for bid in ids:
            b = bibs.get(bid)
            if b is None or bid in seen:
                continue
            seen.add(bid)
            info = b.get("briefInfo", {})
            if info.get("format") not in BC_BOOK_FORMATS:
                continue
            out.append({
                "bib_id": bid,
                "strict": strict,
                "title": info.get("title"),
                "subtitle": info.get("subtitle"),
                "alt_title": (info.get("multiscriptTitle") or {}).get("title")
                             if isinstance(info.get("multiscriptTitle"), dict)
                             else info.get("multiscriptTitle"),
                "authors": info.get("authors") or [],
                "format": info.get("format"),
                "format_class": _bc_format_class(info.get("format")),
                "year": info.get("publicationDate"),
                "call_number": info.get("callNumber"),
                "language": info.get("primaryLanguage"),
            })
    return out


def bc_item_state(avail: dict) -> str:
    if avail.get("libraryUseOnly"):
        return "reference"
    if avail.get("statusType") == "AVAILABLE":
        return "available"
    return "out"


def bc_parse_availability(payload: dict) -> list[dict]:
    """Gateway availability JSON → per-copy dicts for add_remote_availability."""
    items = []
    for it in payload.get("entities", {}).get("bibItems", {}).values():
        avail = it.get("availability", {})
        items.append({
            "branch": it.get("branchName")
                      or (it.get("branch") or {}).get("name") or "?",
            "collection": it.get("collection"),
            "call_number": it.get("callNumber"),
            "status": avail.get("libraryStatus") or avail.get("status"),
            "state": bc_item_state(avail),
        })
    return items


class BiblioCommons:
    """One BiblioCommons library (subdomain = 'sccl' or 'sjpl')."""

    def __init__(self, subdomain: str):
        self.subdomain = subdomain

    def search(self, query: str) -> list[dict]:
        url = (f"{GATEWAY}/{self.subdomain}/bibs/search?"
               f"query={urllib.parse.quote(query)}&searchType=smart")
        return bc_parse_search(json.loads(_get(url)))

    def search_fielded(self, title: str, surname: str = "") -> list[dict]:
        """Boolean field search — smart search drowns classics in spinoffs
        (25 'Very Hungry Caterpillar <theme>' board books before the original).
        """
        q = f"title:({re.sub(r'[():]', ' ', title)})"
        if surname:
            q += f" AND contributor:({surname})"
        url = (f"{GATEWAY}/{self.subdomain}/bibs/search?"
               f"query={urllib.parse.quote(q)}&searchType=bl")
        return bc_parse_search(json.loads(_get(url)), strict=True)

    def availability(self, bib_id: str) -> list[dict]:
        url = f"{GATEWAY}/{self.subdomain}/bibs/{bib_id}/availability"
        return bc_parse_availability(json.loads(_get(url)))


def bc_marc_fields(page: str) -> dict:
    """The classic MARC display (item/catalogue_info) → {tag: [field data]}."""
    out = {}
    for m in re.finditer(r'class="marcTag"><strong>(\d+)</strong></td>.*?'
                         r'class="marcTagData">(.*?)</td>', page, re.S):
        out.setdefault(m.group(1), []).append(
            htmllib.unescape(m.group(2)).strip())
    return out


def _marc_subfields(data: str) -> dict:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"\$([a-z0-9])([^$]*)", data)}


def _marc_subvals(data: str, keep=None, sep: str = " ") -> str:
    """Join a field's subfield values ('$aBears$vFiction' → 'Bears -- Fiction'
    with sep=' -- '); keep=None takes every alphabetic subfield."""
    vals = [(m.group(1), m.group(2).strip())
            for m in re.finditer(r"\$([a-z0-9])([^$]*)", data)]
    return sep.join(v for k, v in vals
                    if v and ((keep is None and k.isalpha()) or
                              (keep and k in keep))).strip(" .,:;/")


# details key -> (MARC tags, subfields to keep, join separator)
_MARC_DETAILS = [
    ("isbn", ("020",), ("a", "q"), " "),
    ("edition", ("250",), ("a", "b"), " "),
    ("publisher", ("264", "260"), ("a", "b", "c"), " "),
    ("phys_desc", ("300",), None, " "),
    ("series", ("490", "830"), ("a", "v"), " "),
    ("summary", ("520",), ("a",), " "),
    ("audience", ("521",), ("a", "b"), " "),
    ("subjects", ("600", "650", "651"), None, " -- "),
    ("genres", ("655",), ("a",), " "),
    ("contributors", ("700",), None, " "),        # illustrators, narrators, …
    ("alt_titles", ("246", "740"), ("a", "b"), " "),
    ("notes", ("500",), ("a",), " "),
    ("awards", ("586",), ("a",), " "),
    ("lccn", ("010",), ("a",), " "),
    ("oclc", ("035",), ("a",), " "),
    ("lcc_call", ("050",), ("a", "b"), " "),
    ("ddc_call", ("082",), ("a",), " "),
    ("call_number", ("099", "092", "090"), None, " "),
    ("reading_program", ("526",), None, " "),     # Accelerated Reader etc.
]


def _marc_details(marc: dict) -> dict:
    """The full bibliographic picture from the MARC display — everything the
    brief search payload doesn't carry."""
    out = {}
    for key, tags, keep, sep in _MARC_DETAILS:
        vals = []
        for tag in tags:
            for d in marc.get(tag, []):
                v = _marc_subvals(d, keep, sep)
                if v and v not in vals:
                    vals.append(v)
        if vals:
            out[key] = vals if len(vals) > 1 else vals[0]
    return out


_BC_BIB_ID = re.compile(r"^S(\d+)C(\d+)$")


def _detail_url(system: str, bib_id: str) -> str | None:
    """The record's full-detail page (BC: the classic MARC display; WebPACs:
    the record view) — the source of everything the search payloads omit."""
    if system in ("sccl", "sjpl"):
        m = _BC_BIB_ID.match(bib_id or "")
        return (f"https://{system}.bibliocommons.com/item/catalogue_info/"
                f"{m.group(2)}{m.group(1)}") if m else None
    if system == "mvpl":
        # the MARC display beats the record view: tagged fields, subfield
        # marks, and data the view never renders (099, 521 Lexile, 526 AR)
        return f"{MVPL_BASE}/search?/.{bib_id}/.{bib_id}/1,1,1,B/marc~{bib_id}"
    if system == "linkplus":
        return (f"{LINKPLUS_BASE}/search?/.{bib_id}/.{bib_id}/1,1,1,B/"
                f"detlframeset~{bib_id}&FF=&1,0,")
    return None


def _edition_details(conn, system: str, bib_id: str, delay: float) -> dict:
    """Full details for one edition, mirror-first: LINK+ detail pages were
    already fetched for holdings, so they parse for free from raw_pages;
    anything not mirrored is fetched live (and thereby mirrored)."""
    url = _detail_url(system, bib_id)
    if not url:
        return {}
    raw = db.get_raw_page(conn, url)
    if raw is None:
        raw = _get(url, accept="text/html",
                   timeout=75 if system in ("mvpl", "linkplus") else 60)
        time.sleep(delay)
    if system in ("sccl", "sjpl"):
        return bc_details_from_page(raw.decode("utf-8", "replace"))
    if system == "mvpl":
        return marc_details_bundle(iii_marc_fields(
            raw.decode("iso-8859-1", "replace")))
    return mvpl_details_from_page(raw.decode("utf-8", "replace"))


def iii_marc_fields(page: str) -> dict:
    """The classic catalog's own MARC display (<pre>, 'TAG II DATA' lines,
    7-space continuations, '|x' subfield marks, implicit first $a) →
    {tag: [field data]}, normalized to the $-convention bc_marc_fields uses.
    """
    m = re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S)
    if not m:
        return {}
    out = {}
    tag, buf = None, ""

    def flush():
        nonlocal tag, buf
        data = htmllib.unescape(buf).strip()
        if tag and data:
            if not data.startswith("|"):
                data = "|a" + data
            out.setdefault(tag, []).append(data.replace("|", "$"))
        tag, buf = None, ""

    for line in m.group(1).splitlines():
        if re.match(r"^\d{3}", line):
            flush()
            tag, buf = line[:3], line[7:]
        elif tag and line.startswith(" "):
            # III wraps long fields; mid-token wraps (URLs) carry no spaces
            buf += line[7:] if len(line) > 7 else line.strip()
    flush()
    return out


def bc_details_from_page(page: str) -> dict:
    return marc_details_bundle(bc_marc_fields(page))


def marc_details_bundle(marc: dict) -> dict:
    parts = []
    for d in marc.get("505", []):
        t = re.sub(r"\$[a-z0-9]", " ", d)
        t = re.sub(r"\s*--\s*", "; ", t)
        t = re.sub(r"\s+", " ", t).strip(" ;$")
        if t:
            parts.append(t)
    orig = ""
    for d in marc.get("240", []):
        orig = _marc_subfields(d).get("a", "").strip(" /:;,.$")
        if orig:
            break
    if not orig:
        for d in marc.get("500", []) + marc.get("765", []):
            m2 = re.search(r"Translation of\s*:?\s*(.+)",
                           re.sub(r"\$[a-z0-9]", " ", d), re.I)
            if m2:
                orig = m2.group(1).strip(" .$")
                break
    if not orig:
        # a translated record's added/uniform title: '<original>. <Language>.'
        for tag in ("730", "740", "246"):
            for d in marc.get(tag, []):
                m3 = _UNIFORM_LANG.match(_marc_subvals(d, ("a",)))
                if m3 and m3.group(1).strip():
                    orig = m3.group(1).strip(" .")
                    break
            if orig:
                break
    return {"contents": "; ".join(parts), "orig_title": orig,
            "details": _marc_details(marc)}


# A translated record's uniform title reads '<original title>. <Language>.'
_UNIFORM_LANG = re.compile(
    r"^(.*?)[.,]?\s*(Chinese|Spanish|French|Japanese|Korean|Vietnamese|"
    r"Russian|German)\.?\s*$", re.I)


def translation_matches_want(want_title: str, want_author: str,
                             orig_title: str) -> bool:
    """Does a record's stated original ('Translation of:' note / uniform
    title) actually name this want?

    The translation rule can't check titles across languages, so a same-author
    record riding a keyword result ('Love is a handful of honey' carries a
    'creators of Giraffes Can't Dance' note) looks identical to a real
    translation — until its own record names what it translates. True notes
    match the want modulo articles and punctuation, or bundle author/language
    around it ('The snowy day by Ezra Jack Keats'); spinoff notes ('Dragons
    love tacos 2') sit just below the edition bar.
    """
    orig = re.sub(r"^[\s:.]+", "", orig_title or "").strip()
    if not orig:
        return True     # nothing to check against — keep; the label discloses

    def flat(s):
        s = re.split(r"[:：=/]", s or "")[0]
        s = re.sub(r"^(the|a|an|el|la|los|las|le|les|un|une)\s+", "",
                   s.strip(), flags=re.I)
        return re.sub(r"[^a-z0-9一-鿿]", "", _norm(s))

    wn, on = flat(want_title), flat(orig)
    if wn and wn == on:
        return True
    if title_score(want_title, [orig]) >= EDITION_MIN_RATIO:
        return True
    surname = query_terms("", want_author)[1] if want_author else ""
    if wn and wn in on and surname and \
            re.sub(r"[^a-z0-9]", "", _norm(surname)) in on:
        return True
    return False


# --- Mountain View classic WebPAC -----------------------------------------------

MVPL_BASE = "https://classiccatalog.mountainview.gov"

# Statuses that mean "walk in and it's on the shelf". Everything else (DUE …,
# ON HOLDSHELF, IN TRANSIT, MISSING, …) counts as out.
_WEBPAC_AVAILABLE = ("AVAILABLE", "CHECK SHELF", "NEW SHELF")
_WEBPAC_REFERENCE = ("LIB USE ONLY", "REFERENCE", "NON-CIRC")
# checked before the available markers: 'UNAVAILABLE' (LINK+) would otherwise
# hit the 'AVAILABLE' substring
_WEBPAC_OUT = ("UNAVAILABLE", "NOT AVAILABLE")


def webpac_state(status: str) -> str:
    s = (status or "").upper()
    if any(m in s for m in _WEBPAC_REFERENCE):
        return "reference"
    if any(m in s for m in _WEBPAC_OUT):
        return "out"
    if any(m in s for m in _WEBPAC_AVAILABLE):
        return "available"
    return "out"


def _strip_html(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = re.sub(r"\s+", " ", htmllib.unescape(fragment)).strip()
    # keyword-highlight spans leave 'I stink !' / 'McMullan , Kate' behind
    # (':' and ';' stay spaced — ISBD subtitle punctuation is ' : ')
    return re.sub(r"\s+([!?,.])", r"\1", text)


def _webpac_items(chunk: str) -> list[dict]:
    items = []
    for m in re.finditer(r'<tr\s+class="bibItemsEntry">(.*?)</tr>', chunk, re.S):
        cells = [_strip_html(c) for c in
                 re.findall(r"<td[^>]*>(.*?)(?:</td>|$)", m.group(1), re.S)]
        if len(cells) < 3:
            continue
        loc, call, status = cells[0], cells[1], cells[2]
        items.append({"branch": loc, "collection": None, "call_number": call,
                      "status": status, "state": webpac_state(status)})
    return items


# Digital editions we keep — checked first: 'eAudiobook' contains 'Audiobook',
# which contains 'Audio'.
_DIGITAL_MEDIA = re.compile(r"e-?Book|e-?Audio|Downloadable", re.I)
# Physical audiobooks are a format we keep (classed 'audio'); checked before
# _NONBOOK_MEDIA because 'Audiobook' would otherwise trip its 'Audio'.
_AUDIO_MEDIA = re.compile(r"Audiobook|Book on CD|CD Book|Playaway(?!\s*Video)",
                          re.I)
# an exact-title DVD, soundtrack CD, or eBook must never satisfy a want,
# no matter how well the title scores
_NONBOOK_MEDIA = re.compile(r"DVD|Blu-?ray|Compact Dis|\bCD\b|Audio|Video|"
                            r"Playaway|eBook|Magazine|Kit\b|videodisc|sound disc",
                            re.I)
_NONBOOK_SHELF = re.compile(r"Movies|Music", re.I)


def _webpac_nonbook(media: str, items: list[dict]) -> bool:
    m = media or ""
    if not _DIGITAL_MEDIA.search(m) and not _AUDIO_MEDIA.search(m) \
            and _NONBOOK_MEDIA.search(m):
        return True
    # a record whose every copy shelves under Movies/Music is one of those,
    # whatever it calls itself
    return bool(items) and all(_NONBOOK_SHELF.search(i.get("branch") or "")
                               for i in items)


# MVPL flags language in the call number: 'J FRENCH J P TISON', 'J CHINESE …'
_WEBPAC_LANGS = {"FRENCH": "fre", "CHINESE": "chi", "SPANISH": "spa",
                 "JAPANESE": "jpn", "KOREAN": "kor", "RUSSIAN": "rus",
                 "GERMAN": "ger", "HINDI": "hin"}


def _webpac_language(items: list[dict]) -> str | None:
    blob = " ".join((i.get("call_number") or "").upper() for i in items)
    for marker, code in _WEBPAC_LANGS.items():
        if marker in blob:
            return code
    return None


def _webpac_fmt_class(media: str, items: list[dict]) -> str:
    if _DIGITAL_MEDIA.search(media or ""):
        return "eaudio" if re.search("audio", media, re.I) else "ebook"
    if _AUDIO_MEDIA.search(media or ""):
        return "audio"
    blob = f"{media} " + " ".join(i["call_number"] or "" for i in items)
    if "board" in blob.lower():
        return "board"
    if "picture" in blob.lower():
        return "picture"
    return "book"


def _webpac_fields(page: str) -> dict:
    """Record-view metadata: bibInfoLabel → cleaned bibInfoData text."""
    fields = {}
    for m in re.finditer(r'<td[^>]*class="bibInfoLabel">\s*([^<]+?)\s*</td>\s*'
                         r'<td[^>]*class="bibInfoData">(.*?)</td>', page, re.S):
        fields.setdefault(m.group(1).strip(), _strip_html(m.group(2)))
    return fields


def _webpac_fields_all(page: str) -> dict:
    """Every value per label. Classic WebPAC repeats a field two ways — an
    extra row whose label cell is EMPTY ('Subject' then two blank-labeled
    rows), and values stacked inside one cell with <br> (two ISBNs) — and
    both continuation forms belong to the preceding label."""
    fields, last = {}, None
    for m in re.finditer(r'<td[^>]*class="bibInfoLabel">\s*([^<]*?)\s*</td>\s*'
                         r'<td[^>]*class="bibInfoData">(.*?)</td>', page, re.S):
        label = m.group(1).strip() or last
        if not label:
            continue
        last = label
        for piece in re.split(r"<br\s*/?>", m.group(2), flags=re.I):
            v = _strip_html(piece)
            if v:
                fields.setdefault(label, []).append(v)
    return fields


def _webpac_record_page(page: str) -> dict | None:
    """A single-hit keyword search jumps straight to the record view; parse that.

    The record page lays metadata out as bibInfoLabel/bibInfoData pairs and has
    the same bibItems table the results list embeds.
    """
    fields = _webpac_fields(page)
    title_stmt = fields.get("Title")
    if not title_stmt:
        return None
    # 'I stink! / Kate & Jim McMullan ; pictures by ...' → title / responsibility
    title = re.split(r"\s+/\s+", title_stmt)[0].strip()
    author = fields.get("Author", "")
    bid = None
    m = re.search(r"record=(b\d+)", page)
    if m:
        bid = m.group(1)
    items = _webpac_items(page)
    if _webpac_nonbook(fields.get("Material", ""), items):
        return None
    return {"bib_id": bid, "strict": True, "title": title, "subtitle": None,
            "alt_title": None, "authors": [author] if author else [],
            "format": fields.get("Material", "Book"),
            "format_class": _webpac_fmt_class(fields.get("Material", ""), items),
            "year": None, "items": items,
            "language": _webpac_language(items)}


def webpac_parse_results(page: str) -> list[dict]:
    """Keyword-results page → candidates, each carrying its own item rows.

    The classic WebPAC inlines every hit's LOCATION/CALL #/STATUS table right in
    the results list, so one request answers both 'is it there' and 'where'.
    A single-hit search renders the record view instead — handled as one candidate.
    """
    chunks = re.split(r'class="briefCitRow"', page)[1:]
    if not chunks and 'class="bibItems"' in page:
        rec = _webpac_record_page(page)
        return [rec] if rec else []
    out = []
    for chunk in chunks:
        m = re.search(r'<span class="briefcitTitle">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        title = _strip_html(m.group(1))
        author = ""
        m2 = re.search(r"</span>\s*<br\s*/?>\s*([^<]*)<br", chunk[m.end():], re.S)
        if m2:
            author = _strip_html(m2.group(1))
        bid = None
        m3 = re.search(r'name="save"\s+value="(b\d+)"', chunk)
        if m3:
            bid = m3.group(1)
        media = ""
        m4 = re.search(r'/screens/media_[a-z_]+\.gif"\s+alt="([^"]*)"', chunk)
        if m4:
            media = m4.group(1)
        items = _webpac_items(chunk)
        if _webpac_nonbook(media, items):
            continue
        out.append({"bib_id": bid, "strict": True, "title": title,
                    "subtitle": None, "alt_title": None,
                    "authors": [author] if author else [],
                    "format": media or "Book",
                    "format_class": _webpac_fmt_class(media, items),
                    "year": None, "items": items,
                    "language": _webpac_language(items)})
    return out


def webpac_query(query: str) -> str:
    """What this catalog's search box can actually digest.

    - CJK 502s the 2006-era server — Chinese records are searchable only
      through their romanization.
    - Diacritics must be folded, not stripped ('Bébés' → 'bebes', not 'b b s').
    - Apostrophes must be joined, not split: the III keyword index matches
      "can't" to 'cant', while a split leaves a stray 't' (or the 'T' of
      T'choupi) that ANDs the search down to nothing.
    - Other punctuation goes: '?' is a truncation wildcard here, and 'see?'
      quietly turns an exact search into garbage matches.
    """
    if _CJK.search(query):
        query = _pinyin(query)
    query = unicodedata.normalize("NFKD", query)
    query = "".join(c for c in query if not unicodedata.combining(c))
    query = re.sub(r"['’]", "", query)
    query = re.sub(r"[^A-Za-z0-9 ]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    # mid-query 'not' is a boolean operator here — 'But not the hippopotamus'
    # finds nothing. A *leading* 'not' has nothing to negate and stays a term.
    toks = query.split()
    if toks:
        query = " ".join([toks[0]] + [t for t in toks[1:] if t.lower() != "not"])
    return query


class MountainView:
    """Mountain View Public Library via its classic (server-rendered) WebPAC."""

    def search(self, query: str) -> list[dict]:
        url = (f"{MVPL_BASE}/search~S1/?searchtype=X"
               f"&searcharg={urllib.parse.quote_plus(webpac_query(query))}&SORT=D")
        # cold keyword searches here can take >30s; be patient
        page = _get(url, accept="text/html", timeout=75).decode("iso-8859-1",
                                                                "replace")
        return webpac_parse_results(page)

    def availability(self, bib_or_cand) -> list[dict]:
        # items ride along with the search results — no second request needed
        return bib_or_cand.get("items", [])


def mvpl_details_from_page(page: str) -> dict:
    fields = _webpac_fields(page)
    contents = re.sub(r"\s*--\s*", "; ",
                      fields.get("Contents", "")).strip(" ;")
    orig = ""
    # scan every metadata cell: the translation note is one of several 'Note'
    # rows and _webpac_fields keeps only the first per label
    for m in re.finditer(r'class="bibInfoData">(.*?)</td>', page, re.S):
        m2 = re.match(r"Translation of\s*:?\s*(.+)",
                      _strip_html(m.group(1)), re.I)
        if m2:
            orig = m2.group(1).strip(" .")
            break
    if not orig:
        # no note — the uniform title ('Love is a handful of honey. Chinese.')
        # names the original just as well
        for key in ("Uniform Title", "Add Title", "Other Title"):
            m3 = _UNIFORM_LANG.match(fields.get(key, ""))
            if m3 and m3.group(1).strip():
                orig = m3.group(1).strip(" .")
                break
    allf = _webpac_fields_all(page)

    def pick(*keys):
        vals = []
        for k in keys:
            vals += [v for v in allf.get(k, []) if v not in vals]
        return (vals if len(vals) > 1 else vals[0]) if vals else None

    details = {k: v for k, v in {
        "isbn": pick("ISBN", "ISBN/ISSN"),
        "edition": pick("Edition"),
        "publisher": pick("Publication", "Imprint"),
        "phys_desc": pick("Material", "Descript", "Description"),
        "summary": pick("Summary"),
        "audience": pick("Audience", "Target Audience"),
        "series": pick("Series"),
        "subjects": pick("Subject"),
        "genres": pick("Genre"),
        "alt_titles": pick("Add Title", "Other Title", "Alt Title",
                           "Uniform Title"),
        "authors": pick("Author"),
        "contributors": pick("Added Entry", "Alt Author"),
        "notes": pick("Note"),
        "awards": pick("Awards", "Award"),
    }.items() if v}
    return {"contents": contents, "orig_title": orig, "details": details}


# --- LINK+ (INN-Reach union catalog) --------------------------------------------

LINKPLUS_BASE = "https://csul.iii.com"


def linkplus_parse_results(page: str) -> list[dict]:
    """LINK+ results list → candidates (no items; holdings need a second fetch).

    Same WebPAC family as Mountain View but the 2009-era INN-Reach skin:
    div.briefcitRow (lower-case c), h2.briefcitTitle, no media icons — non-book
    formats are visible only in the description line ('1 videodisc …').
    """
    out = []
    for chunk in re.split(r'class="briefcitRow"', page)[1:]:
        m = re.search(r'<h2 class="briefcitTitle">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        title = _strip_html(m.group(1))
        author = ""
        m2 = re.search(r"<br\s*>\s*([^<]*)<br", chunk[m.end():], re.S)
        if m2:
            author = _strip_html(m2.group(1))
        m3 = re.search(r'name="save"\s+value="(b\d+)"', chunk)
        bid = m3.group(1) if m3 else None
        desc = _strip_html(chunk[m.end():m.end() + 800])
        if _NONBOOK_MEDIA.search(desc):
            continue
        out.append({"bib_id": bid, "strict": True, "title": title,
                    "subtitle": None, "alt_title": None,
                    "authors": [author] if author else [],
                    "format": "Book", "format_class": "book",
                    "year": None, "language": None})
    return out


def linkplus_parse_holdings(page: str) -> list[dict]:
    """centralDetailHoldings rows → per-copy dicts (branch = owning library)."""
    items = []
    m = re.search(r'<table[^>]*class="centralDetailHoldings".*?</table>', page, re.S)
    if not m:
        return items
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        cells = [_strip_html(c) for c in
                 re.findall(r"<td[^>]*>(.*?)(?:</td>|$)", row.group(1), re.S)]
        if len(cells) < 5 or not cells[0]:
            continue
        library, shelf, _link, call, status = cells[:5]
        items.append({"branch": library, "collection": shelf,
                      "call_number": call, "status": status,
                      "state": webpac_state(status)})
    return items


class LinkPlus:
    """LINK+ union catalog: any hit is requestable for pickup at a member library."""

    def search(self, query: str) -> list[dict]:
        url = (f"{LINKPLUS_BASE}/search~S0/?searchtype=X"
               f"&searcharg={urllib.parse.quote_plus(webpac_query(query))}&SORT=D")
        # unlike MVPL's latin-1 WebPAC, the INN-Reach central serves UTF-8
        page = _get(url, accept="text/html", timeout=75).decode("utf-8",
                                                                "replace")
        cands = linkplus_parse_results(page)
        if not cands and "centralDetailHoldings" in page:
            rec = _webpac_record_page(page)  # single hit → detail view
            if rec:
                rec["items"] = linkplus_parse_holdings(page)
                return [rec]
        return cands

    def availability(self, cand) -> list[dict]:
        if isinstance(cand, dict):
            if cand.get("items") is not None:
                return cand["items"]
            cand = cand["bib_id"]
        bib = cand
        url = (f"{LINKPLUS_BASE}/search?/.{bib}/.{bib}/1,1,1,B/"
               f"detlframeset~{bib}&FF=&1,0,")
        page = _get(url, accept="text/html", timeout=75).decode("utf-8",
                                                                "replace")
        return linkplus_parse_holdings(page)


SYSTEMS = {
    "sccl": ("Santa Clara County Library District", lambda: BiblioCommons("sccl")),
    "sjpl": ("San José Public Library", lambda: BiblioCommons("sjpl")),
    "mvpl": ("Mountain View Public Library", MountainView),
    "linkplus": ("LINK+ union catalog", LinkPlus),
}

# Run only when named. Since 2026-09-12 Mountain View's WebPAC returns 403 for
# any search not submitted from its own search page, and robots.txt on it and
# on its Vega catalog disallows crawlers: a deliberate gate, so it is not worked
# around. Its record links still load, so the reports keep them.
SKIPPED_SYSTEMS = {
    "mvpl": "Mountain View's catalog has refused scripted searches since "
            "2026-09-12, and its robots.txt disallows crawlers",
}
DEFAULT_SYSTEMS = [s for s in sorted(SYSTEMS) if s not in SKIPPED_SYSTEMS]
# A system whose searches fail this many titles in a row is abandoned for the
# run: it is blocked or down, and back-off on every remaining title would only
# stall the systems that are working.
MAX_CONSECUTIVE_FAILURES = 3

# Politeness per host, applied within each system's own (serial) thread —
# LINK+ 429s below a full second; the others tolerate a brisker pace.
SYSTEM_DELAYS = {"sccl": 0.4, "sjpl": 0.4, "mvpl": 0.4, "linkplus": 1.0}
DISCOVERY_MAX_AGE = timedelta(days=7)
# These adapters can poll a saved bib ID without re-running a search.
DISCOVERY_CACHE_SYSTEMS = {"sccl", "sjpl", "linkplus"}


# --- runner ---------------------------------------------------------------------

WANTLIST_GLOB = "wantlist_*.json"  # wantlist_zh.json, wantlist_fr.json, …
EXCLUDE_FILE = "wantlist_exclude.json"


def load_excludes(path: str = EXCLUDE_FILE) -> set:
    """Exact DB titles to leave out of remote lookups (Peoria-only shelf finds)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh).get("titles", []))
    except FileNotFoundError:
        return set()


def sync_wantlist(conn, path: str) -> int:
    """Merge a hand-curated want-list file (checked into git) into `titles`.

    These books have no Peoria record (yet), so they get synthetic WANT: ids;
    they ride along in every Bay Area lookup and in the generated markdown.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)
    except FileNotFoundError:
        return 0
    ts = datetime.now().isoformat(timespec="seconds")
    for e in entries:
        rid = "WANT:" + re.sub(r"\s+", "", e["title"])
        db.upsert_title(conn, rid, e["title"], ts,
                        {"author": e.get("author") or None,
                         "format": e.get("format"),
                         "isbns": e.get("isbn") or None})
    return len(entries)


def wantlist_langs() -> dict:
    """{record_id: lang} for every want-list entry that pins a language."""
    langs = {}
    for wl in sorted(glob.glob(WANTLIST_GLOB)):
        if wl == EXCLUDE_FILE:
            continue
        with open(wl, encoding="utf-8") as fh:
            for e in json.load(fh):
                if e.get("lang"):
                    langs["WANT:" + re.sub(r"\s+", "", e["title"])] = e["lang"]
    return langs


def excluded_edition(edition):
    if edition.get("language") in EXCLUDED_LANGS:
        return True
    details = edition.get("details") or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = {}
    notes = details.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    # Some LINK+ records omit a language field but state the text languages.
    # Anchor at the start: "Translated from Spanish" is not Spanish text.
    return any(re.match(
        r"^(?:(?:parallel\s+)?texts?\s+(?:in\s+)?|parallel\s+)?"
        r"(?:(?:English|Chinese)\s+(?:and|&)\s+)?"
        r"(?:Spanish|Japanese|French)\b", part.strip(), re.I)
        for note in notes for part in note.split("="))


def _drop_excluded_edition(conn, system, record_id, bib_id):
    conn.execute("DELETE FROM remote_bibs WHERE system=? AND record_id=? AND bib_id=?",
                 (system, record_id, bib_id))
    db.delete_remote_edition(conn, system, record_id, bib_id)


def prune_excluded_editions(conn):
    """Stop polling excluded languages without discarding observation history.

    A removed primary is left unresolved so discovery can find a replacement;
    it must not become a cached miss. Other cached editions keep their age.
    """
    excluded = [e for e in db.remote_editions(conn) if excluded_edition(dict(e))]
    for e in excluded:
        _drop_excluded_edition(conn, e["system"], e["record_id"], e["bib_id"])
    return len(excluded)


def _discovery_key(row, lang):
    # Bump the version when matching rules change enough to require re-search.
    return json.dumps([1, row["title"], row["author"], row["format"],
                       row["isbns"], lang], ensure_ascii=False)


def _seed_discovery_keys(conn, langs):
    """Adopt existing matches using stored inputs, before syncing want-list edits.

    Keep their original search date: adoption must not extend the cache TTL.
    """
    rows = conn.execute("""
        SELECT rb.system, t.record_id, t.title, t.author, t.format, t.isbns
        FROM remote_bibs rb JOIN titles t USING(record_id)
        WHERE rb.query_key IS NULL
    """).fetchall()
    for row in rows:
        conn.execute("UPDATE remote_bibs SET query_key=? WHERE system=? AND record_id=?",
                     (_discovery_key(row, langs.get(row["record_id"])),
                      row["system"], row["record_id"]))


def lookup_all(db_path: str, systems: list[str], limit: int = None,
               delay: float = None, resume: bool = False,
               retry_misses: bool = False, rediscover: bool = False) -> None:
    """Look the want-list up at every requested system — systems in parallel
    (one thread + one DB connection each; each host still gets serial,
    delay-spaced requests), then the detail-enrichment pass, then the reports.
    """
    set_archive(db_path)
    conn = db.open_db(db_path)
    langs = wantlist_langs()
    with conn:
        prune_excluded_editions(conn)
        _seed_discovery_keys(conn, langs)
    for wl in sorted(glob.glob(WANTLIST_GLOB)):
        if wl == EXCLUDE_FILE:
            continue
        n = sync_wantlist(conn, wl)
        if n:
            conn.commit()
            print(f"want-list: merged {n} entries from {wl}")
    rows = conn.execute(
        "SELECT record_id, title, author, format, isbns FROM titles ORDER BY title"
    ).fetchall()
    excludes = load_excludes()
    if excludes:
        before = len(rows)
        rows = [r for r in rows if r["title"] not in excludes]
        print(f"want-list: excluding {before - len(rows)} titles "
              f"({EXCLUDE_FILE})")
    if limit:
        rows = rows[:limit]
    conn.close()

    threads = []
    for system in systems:
        d = delay if delay is not None else SYSTEM_DELAYS.get(system, 0.4)
        t = threading.Thread(target=_lookup_system, name=system,
                             args=(db_path, system, rows, langs, d,
                                   resume, retry_misses, rediscover))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    n = enrich_editions(db_path, systems, delay if delay is not None else 0.5)
    if n:
        print(f"details: enriched {n} compilation/translation records")
    for path in report.write_bayarea(db_path):
        print(f"wrote {path}")


def _discover(client, system, row, lang, delay, conn=None):
    """Search and match editions; status-only refreshes skip this step."""
    t, surname = query_terms(row["title"], row["author"])
    query = t if _CJK.search(t) else f"{t} {surname}".strip()
    cands = client.search(query)
    time.sleep(delay)
    if not cands and query != t:
        # author surname can over-constrain an AND search; retry bare
        cands = client.search(t)
        time.sleep(delay)
    if not cands and _CJK.search(t):
        # some records are only findable through their romanization
        cands = client.search(_pinyin(t))
        time.sleep(delay)
    if not cands and row["isbns"]:
        # last resort: the known edition's ISBN (candidates still
        # have to pass title scoring, so a stale ISBN is harmless)
        for isbn in re.findall(r"[0-9Xx]{10,13}", row["isbns"]):
            cands = client.search(isbn)
            time.sleep(delay)
            if cands:
                break
    enforce = system != "linkplus"
    if hasattr(client, "search_fielded"):
        # always pool in the boolean field search: it rescues weak
        # smart-search picks AND is the only strict source for
        # translations/audiobooks (pick_all trusts nothing else)
        fcands = client.search_fielded(t, surname)
        time.sleep(delay)
        known = {c.get("bib_id") for c in cands}
        fids = {c.get("bib_id") for c in fcands}
        for c in cands:      # a bib in both sets is strict
            if c.get("bib_id") in fids:
                c["strict"] = True
        cands = cands + [c for c in fcands
                         if c.get("bib_id") not in known]
    # LINK+ holdings cost a page fetch per edition; keep it tight
    if system == "linkplus" and conn is not None:
        # LINK+ search omits language. Reuse already mirrored detail notes to
        # avoid selecting (and polling) a known excluded bilingual record.
        eligible = []
        for cand in cands:
            raw = db.get_raw_page(conn, _detail_url(system, cand.get("bib_id")))
            if raw is not None and excluded_edition({"details":
                    mvpl_details_from_page(raw.decode("utf-8", "replace")).get("details")}):
                continue
            eligible.append(cand)
        cands = eligible
    max_ed = 4 if system == "linkplus" else MAX_EDITIONS
    best, score, editions = pick_all(row["title"], row["author"],
                                     row["format"], cands, lang,
                                     enforce, max_ed)
    return best, score, editions


def _lookup_system(db_path: str, system: str, rows, langs: dict, delay: float,
                   resume: bool, retry_misses: bool, rediscover: bool = False) -> None:
    conn = db.open_db(db_path)
    try:
        label, make_client = SYSTEMS[system]
        client = make_client()
        with conn:
            prune_excluded_editions(conn)
            _seed_discovery_keys(conn, langs)
        matches = {r["record_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM remote_bibs WHERE system=?", (system,))}
        saved_editions = {}
        for ed in conn.execute("SELECT * FROM remote_editions WHERE system=?", (system,)):
            saved_editions.setdefault(ed["record_id"], []).append(dict(ed))
        availability_cache = {}  # successful responses only; discarded after this run
        searched = reused = cached_misses = availability_requests = 0
        todo = [r for r in rows if langs.get(r["record_id"]) not in EXCLUDED_LANGS]
        if resume or retry_misses:
            # resume: skip anything already looked up (crash recovery).
            # retry_misses: also redo titles that were searched but never matched.
            q = "SELECT record_id FROM remote_bibs WHERE system = ?"
            if retry_misses:
                q += " AND bib_id IS NOT NULL"
            done = {r["record_id"] for r in conn.execute(q, (system,))}
            todo = [r for r in todo if r["record_id"] not in done]
        if not todo:
            print(f"[{system}] nothing to do")
            return
        checked_at = datetime.now().isoformat(timespec="seconds")
        with conn:  # commit at once — an open write transaction stalls the others
            scrape_id = db.record_scrape(conn, kind="remote",
                                         checked_at=checked_at,
                                         query=f"{len(todo)} titles",
                                         source="bayarea_lookup", profile=system)
        print(f"[{system}] {label}: {len(todo)} titles")
        failures = 0
        for i, row in enumerate(todo, 1):
            if failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[{system}] giving up: {failures} titles in a row failed "
                      f"— skipping the remaining {len(todo) - i + 1} this run")
                break
            lang = langs.get(row["record_id"])
            key = _discovery_key(row, lang)
            previous = matches.get(row["record_id"])
            cached_editions = saved_editions.get(row["record_id"], [])
            reuse = (system in DISCOVERY_CACHE_SYSTEMS and not rediscover
                     and not retry_misses and previous is not None
                     and not _references_other_work(query_terms(row["title"])[0], previous["title"])
                     and previous["query_key"] == key
                     and datetime.now() - datetime.fromisoformat(previous["checked_at"])
                         < DISCOVERY_MAX_AGE
                     and (previous["bib_id"] is None or bool(cached_editions)))
            try:
                if reuse:
                    if previous["bib_id"] is None:
                        cached_misses += 1
                        print(f"[{system}] {i:3}/{len(todo)} · {row['title'][:50]!r} "
                              "cached miss (weekly discovery)")
                        continue
                    reused += 1
                    best, score, editions = dict(previous), previous["match_score"], cached_editions
                else:
                    searched += 1
                    best, score, editions = _discover(client, system, row, lang, delay, conn)
                if best is None:
                    checked_at = datetime.now().isoformat(timespec="seconds")
                    with conn:
                        db.upsert_remote_bib(conn, system, row["record_id"],
                                             checked_at, None, score, query_key=key)
                        db.replace_remote_editions(conn, system,
                                                   row["record_id"], [],
                                                   checked_at)
                        db.record_remote_snapshot(conn, scrape_id, system,
                                                  row["record_id"], checked_at)
                    print(f"[{system}] {i:3}/{len(todo)} ✗ {row['title'][:50]!r} "
                          f"no match (best {score})")
                    failures = 0
                    continue
                editions = [dict(e, title=_display_title(e)) for e in editions]
                # Fetch every edition before publishing anything. A failure
                # must leave the previous complete title snapshot intact.
                fetched = []
                n_avail = n_items = 0
                for ed in editions:
                    if (ed.get("format_class") or "") in DIGITAL_CLASSES:
                        continue
                    bid = ed["bib_id"]
                    if bid not in availability_cache:
                        availability_requests += 1
                        items = client.availability(ed if system in ("mvpl", "linkplus")
                                                    else bid)
                        observed_at = datetime.now().isoformat(timespec="seconds")
                        availability_cache[bid] = (items, observed_at)
                        if system != "mvpl":
                            time.sleep(delay)
                    items, observed_at = availability_cache[bid]
                    fetched.append((bid, items, observed_at))
                    n_avail += sum(1 for it in items if it["state"] == "available")
                    n_items += len(items)
                # Use the oldest observation in this complete title snapshot;
                # a shared bib must not acquire a newer date when reused.
                checked_at = min((ts for _, _, ts in fetched),
                                 default=datetime.now().isoformat(timespec="seconds"))
                with conn:
                    if not reuse:
                        db.upsert_remote_bib(conn, system, row["record_id"], checked_at,
                                         {"bib_id": best["bib_id"],
                                          "title": _display_title(best),
                                          "author": ", ".join(best["authors"]) or None,
                                          "format": best["format"],
                                          "year": best.get("year")}, score, query_key=key)
                        db.replace_remote_editions(conn, system, row["record_id"],
                                                   editions, checked_at)
                    db.record_remote_snapshot(conn, scrape_id, system,
                                              row["record_id"], checked_at)
                    for bib_id, items, _ in fetched:
                        db.add_remote_availability(conn, scrape_id, system,
                                                   row["record_id"], bib_id,
                                                   row["title"], items, checked_at)
                extras = ", ".join(
                    "+" + ((e.get("language") or "?") if e["kind"] == "translation"
                           else e.get("format_class") or "ed")
                    for e in editions if e["kind"] != "primary")
                print(f"[{system}] {i:3}/{len(todo)} ✓ {row['title'][:50]!r} → "
                      f"{_display_title(best)[:40]!r} ({best['format']}, {score}) "
                      f"{n_avail}/{n_items} on shelf"
                      + (f" [{extras}]" if extras else ""))
                failures = 0
            except Exception as e:
                if reuse:
                    # A saved ID may have been removed or merged. Keep the
                    # previous snapshot, but repair its mapping on the next run.
                    with conn:
                        conn.execute("UPDATE remote_bibs SET query_key='' "
                                     "WHERE system=? AND record_id=?",
                                     (system, row["record_id"]))
                failures += 1
                print(f"[{system}] {i:3}/{len(todo)} ! {row['title'][:50]!r} "
                      f"ERROR: {e}")
        conn.commit()
        print(f"[{system}] {searched} titles searched, {reused} matches reused, "
              f"{cached_misses} cached misses; {availability_requests} availability "
              "lookups (successful repeats reused)")
    finally:
        conn.close()


def enrich_editions(db_path: str, systems, delay: float = 0.5) -> int:
    """Fetch the full record details for EVERY tracked edition: the complete
    bibliographic picture (isbn/edition/publisher/summary/audience/series/
    subjects/genres…), a compilation's contents (505), and a translation's
    stated original (uniform title / note) — which is also verified here.
    Idempotent (only rows never fetched are hit), mirror-first (a page already
    in raw_pages costs nothing), and parallel per system, like the lookups.
    """
    set_archive(db_path)
    conn = db.open_db(db_path)
    with conn:
        prune_excluded_editions(conn)
    langs = wantlist_langs()
    todo = [r for r in db.unenriched_editions(conn) if r["system"] in systems
            and langs.get(r["record_id"]) not in EXCLUDED_LANGS]
    wants = {r["record_id"]: (r["title"], r["author"])
             for r in conn.execute("SELECT record_id, title, author FROM titles")}
    conn.close()
    by_sys = {}
    for r in todo:
        by_sys.setdefault(r["system"], []).append(r)
    done = []

    def work(system, rows_):
        c = db.open_db(db_path)
        d_sys = max(delay, SYSTEM_DELAYS.get(system, delay))
        try:
            for r in rows_:
                try:
                    d = _edition_details(c, system, r["bib_id"], d_sys)
                except Exception as e:
                    print(f"  enrich ! {system}/{r['bib_id']}: {e}")
                    continue
                want_t, want_a = wants.get(r["record_id"], ("", ""))
                if excluded_edition({"details": d.get("details")}):
                    with c:
                        _drop_excluded_edition(c, system, r["record_id"], r["bib_id"])
                    continue
                if r["kind"] == "translation" and not translation_matches_want(
                        want_t, want_a, d.get("orig_title")):
                    # the record itself says it translates a different work
                    with c:
                        db.delete_remote_edition(c, system, r["record_id"],
                                                 r["bib_id"])
                    print(f"  enrich ✂ {system}: {want_t[:34]!r} is not "
                          f"{d.get('orig_title')[:40]!r} — dropped")
                    continue
                with c:
                    db.set_edition_details(
                        c, system, r["record_id"], r["bib_id"],
                        d.get("contents"), d.get("orig_title"),
                        json.dumps(d.get("details") or {}, ensure_ascii=False))
                done.append(1)
        finally:
            c.close()

    threads = [threading.Thread(target=work, args=(s, rs), name=f"enrich-{s}")
               for s, rs in by_sys.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(done)


def probe(systems: list[str], query: str) -> None:
    """Ad-hoc one-title lookup; prints, records no lookup results (fetched
    pages still land in the raw mirror)."""
    for system in systems:
        label, make_client = SYSTEMS[system]
        client = make_client()
        print(f"== {label}")
        cands = client.search(query)
        if hasattr(client, "search_fielded"):   # same union a real run pools
            fcands = client.search_fielded(query)
            known = {c.get("bib_id") for c in cands}
            fids = {c.get("bib_id") for c in fcands}
            for c in cands:
                if c.get("bib_id") in fids:
                    c["strict"] = True
            cands += [c for c in fcands if c.get("bib_id") not in known]
        best, score, editions = pick_all(query, None, None, cands)
        if best is None:
            print(f"  no match (best score {score})")
            continue
        for ed in editions:
            items = client.availability(ed if system in ("mvpl", "linkplus")
                                        else ed["bib_id"])
            tag = ed["kind"] + (f":{ed['language']}" if ed.get("language") else "")
            print(f"  {_display_title(ed)!r} [{tag}] ({ed['format']}, "
                  f"score {ed['match_score']})")
            for it in items:
                mark = {"available": "✓", "reference": "·"}.get(it["state"], "✗")
                print(f"   {mark} {it['branch']}: {it['call_number']} — "
                      f"{it['status']}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Look up the want-list at Bay Area library systems.")
    ap.add_argument("--system", action="append", choices=sorted(SYSTEMS),
                    help="limit to a system (repeatable; default: every system "
                         "except " + ", ".join(sorted(SKIPPED_SYSTEMS)) + ")")
    ap.add_argument("--limit", type=int, help="only the first N titles (testing)")
    ap.add_argument("--delay", type=float, default=None,
                    help="seconds between requests, same for every system "
                         "(default: per-system — 0.4, but 1.0 for LINK+)")
    ap.add_argument("--resume", action="store_true",
                    help="skip titles already looked up in that system")
    ap.add_argument("--retry-misses", action="store_true",
                    help="like --resume, but also redo titles that never matched")
    ap.add_argument("--rediscover", action="store_true",
                    help="force catalog searches instead of reusing seven-day matches")
    ap.add_argument("--title", help="ad-hoc query: print availability, touch nothing")
    ap.add_argument("--enrich", action="store_true",
                    help="only fetch missing compilation/translation details, "
                         "then rewrite the reports")
    ap.add_argument("--db", default="shelfwalk.db")
    args = ap.parse_args(argv)

    systems = args.system or DEFAULT_SYSTEMS
    for s in systems:
        if s in SKIPPED_SYSTEMS:
            print(f"warning: {s} is skipped by default — {SKIPPED_SYSTEMS[s]}")
    set_archive(args.db)
    if args.title:
        probe(systems, args.title)
    elif args.enrich:
        n = enrich_editions(args.db, systems,
                            delay=args.delay if args.delay is not None else 0.5)
        print(f"details: enriched {n} records")
        for path in report.write_bayarea(args.db):
            print(f"wrote {path}")
    else:
        lookup_all(args.db, systems, limit=args.limit, delay=args.delay,
                   resume=args.resume, retry_misses=args.retry_misses,
                   rediscover=args.rediscover)


if __name__ == "__main__":
    main()
