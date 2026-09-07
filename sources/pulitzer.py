"""Pulitzer — see acclaim_core for the shared machinery."""
from __future__ import annotations

import json
import os
import urllib.parse
from html import unescape as htmlunescape

import catalog_db as db
from acclaim_core import (  # noqa: F401
    BROWSER, HARVEST_DIR, HTTP, WIKIDATA, Source, _arm_archive, _decode_page,
    _fetch, _fetch_note, _flat_name, _kp_title_case, _strip, _warn_if_mostly_failing,
    _wiki_plain, demojibake, parse_credit)

# --- Pulitzer -------------------------------------------------------------------

# Which Pulitzer category maps to which written form. Drama is a play; the
# nonfiction categories are books; Poetry is a collection, not a single poem.
_PULITZER_FORMS = {
    "Fiction": "novel", "Drama": "drama", "History": "nonfiction",
    "Biography": "nonfiction", "General Nonfiction": "nonfiction",
    "Poetry": "poetry",
}


def load_pulitzer(conn) -> int:
    """Load `harvest/pulitzer.json`, produced by the Chrome pass.

    pulitzer.org fingerprint-blocks scripted clients, so the fetch is done by
    JavaScript in a real tab (see `docs/harvesting.md`); this half is pure
    parsing and runs offline against the harvested file.
    """
    path = os.path.join(HARVEST_DIR, "pulitzer.json")
    if not os.path.exists(path):
        db.log_fetch(conn, "pulitzer", False, note=f"{path} missing — Chrome pass needed")
        raise FileNotFoundError(
            f"{path} not found. Run the Chrome harvest first: "
            "uv run acclaim.py browser-plan")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)

    n = 0
    for r in rows:
        credit = parse_credit(demojibake(r.get("raw", "")))
        if not credit["title"]:
            continue                      # 'No award' years
        key = db.upsert_work(conn, credit["title"], credit["author"])
        if _PULITZER_FORMS.get(r.get("category")):
            conn.execute(
                "UPDATE works SET form = COALESCE(form, ?) WHERE work_key = ?",
                (_PULITZER_FORMS[r["category"]], key))
        if db.add_accolade(conn, key, "pulitzer", "award", r["status"],
                           category=r.get("category"), year=r.get("year"),
                           detail=(r.get("citation") or None) or credit["publisher"],
                           url=r.get("person_url")):
            n += 1
    conn.commit()
    db.log_fetch(conn, "pulitzer", True, url="https://www.pulitzer.org/",
                 n_records=n, n_parsed=len(rows), note=f"{len(rows)} raw rows from harvest")
    return n
