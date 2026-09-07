"""Douban annual book lists — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Douban annual book lists (豆瓣年度读书榜单) ----------------------------------

# Douban is a *list*, not a juried award: an editorial year-end selection with
# a community rating attached, so it lands as source_kind='list' with the
# rating in `detail` rather than as winner/nominee.
#
# Worth having despite the extra effort — SCCL and San Jose both hold real
# Chinese-language collections, and `bayarea_lookup` already romanizes CJK to
# pinyin for exactly these searches.
#
# Harvest is manual: book.douban.com serves scripted clients a 2.4KB stub, and
# from inside the page Chrome blocks the automatic download while Private
# Network Access blocks the localhost POST. See docs/harvesting.md — it needs
# one click to allow the download. Files land in harvest/douban/<year>.json.
DOUBAN_DIR = os.path.join(HARVEST_DIR, "douban")
# '[波] 雷沙德·卡普希钦斯基' — a bracketed nationality marker precedes translated
# authors and is not part of the name.
_DOUBAN_NATIONALITY_RE = re.compile(r"^\s*[\[［][^\]］]{1,12}[\]］]\s*")


def clean_douban_author(name: str) -> str:
    return _DOUBAN_NATIONALITY_RE.sub("", (name or "")).strip()


def load_douban(conn) -> int:
    if not os.path.isdir(DOUBAN_DIR):
        db.log_fetch(conn, "douban", False,
                     note=f"{DOUBAN_DIR}/ missing — manual Chrome harvest needed")
        raise FileNotFoundError(
            f"{DOUBAN_DIR}/<year>.json not found. See docs/harvesting.md.")
    n = files = parsed = 0
    for fn in sorted(os.listdir(DOUBAN_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(DOUBAN_DIR, fn), encoding="utf-8") as fh:
            payload = json.load(fh)
        year = payload.get("year")
        year = int(year) if str(year).isdigit() else None
        files += 1
        parsed += len(payload.get("books", []))
        for b in payload.get("books", []):
            title = demojibake((b.get("title") or "").strip())
            if not title:
                continue
            author = clean_douban_author(demojibake(b.get("author")))
            key = db.upsert_work(conn, title, author or None)
            rating = b.get("rating")
            if db.add_accolade(conn, key, "douban", "list", "listed",
                               category="豆瓣年度读书榜单", year=year,
                               detail=(f"douban {rating}" if rating else None),
                               url=f"https://book.douban.com/annual/{year}/"):
                n += 1
        conn.commit()
    db.log_fetch(conn, "douban", files > 0,
                 url="https://book.douban.com/annual/", n_records=n,
                 n_parsed=parsed,
                 note=f"{files} year file(s) from manual harvest")
    return n
