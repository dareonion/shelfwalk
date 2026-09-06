#!/usr/bin/env python3
"""Watch a handful of hot new releases and get into the hold queue early.

The want-list side of this repo answers "is it on the shelf this morning?".
This side answers a different question: a new release by an author you follow
is never on a shelf — every copy is on order — so the only thing that decides
when you read it is *where you are in the queue*, and that is set weeks before
publication.

What the Taipei Story lookup on 2026-09-05 (publication 09-08) showed, and
what this module is shaped around:

  * "Becomes holdable" is the wrong trigger. The record was already
    holdable:true at SCCL with 30 of its 38 copies still on order. For a
    pre-pub title a record is holdable the moment the vendor feed creates it,
    so the event worth catching is THE BIB APPEARING AT ALL.
  * The number that matters is holds-per-copy, not a boolean. Same book, same
    hour: 0.36 at San Jose, 1.58 at SCCL, 4.0 at Mountain View. Queue in the
    wrong system and you wait a season for a book you could have had in a week.
  * Match on ISBN, not on the title. San Jose catalogued it as "Taipei Story
    (Deluxe Limited Edition)", which scored 0.667 against the want-list matcher
    and was thrown away as a miss — the one system where the queue was actually
    worth joining. Publishers' pre-pub records are full of this.

    uv run hotlist.py check              # poll every watched title, log changes
    uv run hotlist.py check --quiet      # only print when something moved (cron)
    uv run hotlist.py status             # current standing, best queue per title
    uv run hotlist.py holds              # what auto-hold WOULD do (dry run)
    uv run hotlist.py holds --place      # actually place them
    uv run hotlist.py add "Title" --author Kuang --isbn 9780063473744

Watchlist lives in `hotlist.json`; `shelfwalk.db` holds the sighting history
and the hold ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime

import bayarea_lookup as B
import catalog_db as db

WATCHLIST_FILE = "hotlist.json"

# Systems we can *watch*. Whether we can also place a hold is a separate
# question (see HOLD_ROUTES) — watching needs no card at all.
WATCH_SYSTEMS = ("sccl", "sjpl", "mvpl")

# Physical formats only, by default: a Libby queue is a licence pool, and its
# 250-deep hold list behaves nothing like a shelf.
DEFAULT_FORMATS = ("book", "audio")

# Don't join a queue that is already hopeless — past this many holds per copy
# the wait is long enough that buying it or waiting for the hype to pass wins.
DEFAULT_MAX_HOLDS_PER_COPY = 3.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "untitled"


# --- watchlist ------------------------------------------------------------------

def load_watchlist(path: str = WATCHLIST_FILE) -> list[dict]:
    """Read and normalize the watchlist, filling in the optional fields."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out = []
    for e in raw:
        if not e.get("title"):
            raise ValueError(f"watchlist entry with no title: {e!r}")
        isbns = e.get("isbns") or ([e["isbn"]] if e.get("isbn") else [])
        out.append({
            "slug": e.get("slug") or slugify(e["title"]),
            "title": e["title"],
            "author": e.get("author"),
            "isbns": [re.sub(r"[^0-9Xx]", "", i) for i in isbns],
            "pub_date": e.get("pub_date"),
            "systems": tuple(e.get("systems") or WATCH_SYSTEMS),
            "formats": tuple(e.get("formats") or DEFAULT_FORMATS),
            "auto_hold": bool(e.get("auto_hold", False)),
            "pickup": e.get("pickup") or {},
            "max_holds_per_copy": float(
                e.get("max_holds_per_copy", DEFAULT_MAX_HOLDS_PER_COPY)),
        })
    return out


def save_watchlist(entries, path: str = WATCHLIST_FILE) -> None:
    """Write back only the keys the file actually carries (no defaults)."""
    slim = []
    for e in entries:
        row = {"title": e["title"]}
        for k in ("author", "pub_date"):
            if e.get(k):
                row[k] = e[k]
        if e.get("isbns"):
            row["isbns"] = e["isbns"]
        if tuple(e["systems"]) != WATCH_SYSTEMS:
            row["systems"] = list(e["systems"])
        if tuple(e["formats"]) != DEFAULT_FORMATS:
            row["formats"] = list(e["formats"])
        if e.get("auto_hold"):
            row["auto_hold"] = True
        if e.get("pickup"):
            row["pickup"] = e["pickup"]
        if e["max_holds_per_copy"] != DEFAULT_MAX_HOLDS_PER_COPY:
            row["max_holds_per_copy"] = e["max_holds_per_copy"]
        slim.append(row)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(slim, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --- probes ---------------------------------------------------------------------

def _norm_isbn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", s or "").upper()


def bc_bibs(subdomain: str, entry: dict) -> list[dict]:
    """Every BiblioCommons record matching this entry, with the bib-level
    availability summary the want-list path never needed.

    Two query routes pooled: one `identifier:` search per known ISBN (exact,
    and the only thing that survives a mangled pre-pub title), plus the
    title+author fielded search to pick up the editions whose own ISBNs we
    don't have.
    """
    queries = [f"identifier:({i})" for i in entry["isbns"]]
    if entry.get("author"):
        queries.append(f"title:({_query_safe(entry['title'])}) "
                       f"AND contributor:({_query_safe(entry['author'])})")
    else:
        queries.append(f"title:({_query_safe(entry['title'])})")

    found, out = set(), []
    for q in queries:
        url = (f"{B.GATEWAY}/{subdomain}/bibs/search?"
               f"query={urllib.parse.quote(q)}&searchType=bl")
        try:
            payload = json.loads(B._get(url))
        except Exception as exc:                      # noqa: BLE001
            print(f"  ! {subdomain}: {q} — {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        for bid, bib in (payload.get("entities", {}).get("bibs", {})).items():
            if bid in found:
                continue
            info = bib.get("briefInfo", {}) or {}
            av = bib.get("availability", {}) or {}
            fmt_class = B._bc_format_class(info.get("format"))
            cand = {
                "bib_id": bid,
                "title": _joined_title(info),
                "format": info.get("format"),
                "format_class": fmt_class,
                "isbns": [_norm_isbn(i) for i in (info.get("isbns") or [])],
                "authors": info.get("authors") or [],
                "year": info.get("publicationDate"),
                "holdable": bool((bib.get("policy") or {}).get("holdable")),
                "status": av.get("status"),
                "copies": av.get("totalCopies"),
                "on_order": av.get("onOrderCopies"),
                "available": av.get("availableCopies"),
                "holds": av.get("heldCopies"),
                "url": f"https://{subdomain}.bibliocommons.com/v2/record/{bid}",
            }
            if not _entry_matches(entry, cand):
                continue
            found.add(bid)
            out.append(cand)
    return out


def _query_safe(s: str) -> str:
    """Strip the punctuation BiblioCommons' boolean parser chokes on."""
    return re.sub(r"[():&|]", " ", s or "").strip()


def _joined_title(info: dict) -> str:
    t = info.get("title") or ""
    return f"{t}: {info['subtitle']}" if info.get("subtitle") else t


_HOLDS_RE = re.compile(
    r"(\d+)\s+holds?\s+on\s+(?:the\s+)?(?:first copy returned of\s+)?(\d+)\s+cop",
    re.I)


def webpac_holds(page: str) -> tuple[int | None, int | None]:
    """(holds, copies) from a classic Innovative record page.

    The phrasing is 'N holds on first copy returned of M copies'; a title with
    a single copy drops the 'first copy returned of' clause, hence the optional
    group. Returns (None, None) when the line is absent, which is what an
    uncontested record looks like.
    """
    m = _HOLDS_RE.search(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def mvpl_bibs(entry: dict) -> list[dict]:
    """Mountain View's WebPAC: ISBN search first, keyword as the fallback."""
    seen, out = set(), []
    searches = [(f"{B.MVPL_BASE}/search~S1/?searchtype=i&searcharg="
                 f"{urllib.parse.quote_plus(i)}&SORT=D") for i in entry["isbns"]]
    kw = B.webpac_query(f"{entry['title']} {entry.get('author') or ''}")
    searches.append(f"{B.MVPL_BASE}/search~S1/?searchtype=X"
                    f"&searcharg={urllib.parse.quote_plus(kw)}&SORT=D")
    for url in searches:
        try:
            page = B._get(url, accept="text/html",
                          timeout=75).decode("iso-8859-1", "replace")
        except Exception as exc:                      # noqa: BLE001
            print(f"  ! mvpl: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        cands = B.webpac_parse_results(page)
        if not cands:
            rec = B._webpac_record_page(page)
            cands = [rec] if rec else []
        for c in cands:
            bid = c.get("bib_id")
            if not bid or bid in seen:
                continue
            cand = {
                "bib_id": bid,
                "title": c.get("title"),
                # the record view's 'Material' row is the physical description
                # on this install ('279 pages : illustrations ; 24 cm'), which
                # is no use as a format label — fall back to the parser's class
                "format": _format_label(c.get("format"),
                                        c.get("format_class")),
                "format_class": c.get("format_class") or _webpac_class(
                    c.get("format")),
                "isbns": [],            # the results page doesn't carry them
                "authors": [],
                "year": None,
                "url": f"{B.MVPL_BASE}/record={bid}",
            }
            # An ISBN search is its own proof of identity; a keyword hit has to
            # earn it on title+author.
            if "searchtype=i" not in url and not _entry_matches(entry, cand):
                continue
            _fill_webpac_state(cand)
            seen.add(bid)
            out.append(cand)
    return out


def _format_label(fmt: str, fmt_class: str) -> str:
    """A short human label. Innovative's 'Material' row is sometimes a real
    format ('Adult Fiction Book') and sometimes the 300 field; only the former
    is worth showing."""
    f = (fmt or "").strip()
    if not f or re.search(r"\d+\s*(pages|p\.|cm|vol)", f, re.I):
        return (fmt_class or "book").capitalize()
    return f[:24]


def _webpac_class(fmt: str) -> str:
    f = (fmt or "").lower()
    if "audio" in f or " cd" in f:
        return "audio"
    if "ebook" in f or "e-book" in f:
        return "ebook"
    return "book"


def _fill_webpac_state(cand: dict) -> None:
    """Fetch the record page for the hold queue and the copy states."""
    try:
        page = B._get(cand["url"], accept="text/html",
                      timeout=75).decode("iso-8859-1", "replace")
    except Exception as exc:                          # noqa: BLE001
        print(f"  ! mvpl record {cand['bib_id']}: {exc}", file=sys.stderr)
        return
    holds, copies = webpac_holds(page)
    items = B._webpac_items(page)
    avail = sum(1 for i in items if i.get("state") == "available")
    cand.update({
        "holdable": True,       # every circulating WebPAC record takes a hold
        "status": "AVAILABLE" if avail else "UNAVAILABLE",
        "holds": holds,
        "copies": copies if copies is not None else (len(items) or None),
        "on_order": sum(1 for i in items
                        if "order" in (i.get("status") or "").lower()) or None,
        "available": avail,
    })


def _entry_matches(entry: dict, cand: dict) -> bool:
    """ISBN is proof; otherwise the title has to contain the watched title and
    the author surname has to appear.

    Deliberately looser than the want-list matcher in `bayarea_lookup`: those
    rules exist to keep 'Grumpy Monkey: Too Many Bugs' from matching 'Grumpy
    Monkey', which is the right call for a series of near-identical board
    books. Here the risk runs the other way — the watchlist is a handful of
    named titles by named authors, and the cost of a miss (no hold placed) is
    far worse than the cost of a stray extra edition in the report.
    """
    want_isbns = set(entry["isbns"])
    if want_isbns and want_isbns & set(cand.get("isbns") or []):
        return True
    ct = _flat(cand.get("title"))
    wt = _flat(entry["title"])
    if not wt or wt not in ct:
        return False
    surname = (entry.get("author") or "").split(",")[0].strip()
    if not surname:
        return True
    blob = _flat(" ".join([cand.get("title") or ""] +
                          [str(a) for a in (cand.get("authors") or [])]))
    return _flat(surname) in blob or not cand.get("authors")


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


PROBES = {
    "sccl": lambda e: bc_bibs("sccl", e),
    "sjpl": lambda e: bc_bibs("sjpl", e),
    "mvpl": mvpl_bibs,
}


# --- the check pass -------------------------------------------------------------

def holds_per_copy(row) -> float | None:
    holds, copies = row["holds"], row["copies"]
    if holds is None or not copies:
        return None
    return round(holds / copies, 2)


def _transitions(prev, cand: dict) -> list[str]:
    """What changed since the last sighting of this bib, in plain words."""
    if prev is None:
        return ["NEW RECORD"]
    events = []
    if not prev["holdable"] and cand.get("holdable"):
        events.append("became holdable")
    if not (prev["available"] or 0) and (cand.get("available") or 0):
        events.append(f"{cand['available']} copy(ies) now available")
    before, after = prev["holds"], cand.get("holds")
    if before is not None and after is not None and after != before:
        pc = cand.get("copies") or 0
        events.append(f"holds {before}→{after}"
                      + (f" on {pc} copies ({round(after / pc, 2)}/copy)"
                         if pc else ""))
    if (prev["copies"] or 0) != (cand.get("copies") or 0):
        events.append(f"copies {prev['copies']}→{cand.get('copies')}")
    return events


def check(db_path: str, watchlist: str = WATCHLIST_FILE, quiet: bool = False,
          only: str = None) -> list[dict]:
    """Poll every watched title at every watched system; return the events."""
    entries = load_watchlist(watchlist)
    if only:
        entries = [e for e in entries if only.lower() in e["slug"]]
    conn = db.open_db(db_path)
    added, retired = db.sync_hotlist(conn, entries)
    if not quiet and (added or retired):
        print(f"watchlist: {added} added, {retired} retired")

    checked_at = _now()
    events = []
    for entry in entries:
        for system in entry["systems"]:
            probe = PROBES.get(system)
            if probe is None:
                continue
            for cand in probe(entry):
                if cand.get("format_class") not in entry["formats"]:
                    continue
                prev = db.previous_sighting(conn, entry["slug"], system,
                                            cand["bib_id"])
                what = _transitions(prev, cand)
                db.add_hot_sighting(conn, entry["slug"], system, cand,
                                    checked_at)
                if what:
                    events.append({"slug": entry["slug"], "title": entry["title"],
                                   "system": system, "bib": cand,
                                   "events": what})
            conn.commit()
    conn.commit()

    # --quiet silences the housekeeping, never the events: the whole point of
    # the timer is that the one run in fifty that found something says so.
    if events:
        for ev in events:
            b = ev["bib"]
            print(f"[{ev['system']}] {ev['title']} — {b.get('format')} "
                  f"{b['bib_id']}")
            for line in ev["events"]:
                print(f"    · {line}")
            print(f"    {b.get('url')}")
    elif not quiet:
        print("no changes")
    return events


# --- reporting ------------------------------------------------------------------

def status(db_path: str, watchlist: str = WATCHLIST_FILE) -> None:
    conn = db.open_db(db_path)
    entries = {e["slug"]: e for e in load_watchlist(watchlist)}
    rows = db.latest_hot_sightings(conn)
    if not rows:
        print("nothing sighted yet — run: uv run hotlist.py check")
        return
    by_slug: dict[str, list] = {}
    for r in rows:
        by_slug.setdefault(r["slug"], []).append(r)

    for slug, sightings in by_slug.items():
        entry = entries.get(slug)
        title = entry["title"] if entry else slug
        pub = f"  (pub {entry['pub_date']})" if entry and entry.get("pub_date") else ""
        print(f"\n{title}{pub}")
        ranked = sorted(sightings,
                        key=lambda r: (holds_per_copy(r) is None,
                                       holds_per_copy(r) or 0))
        for r in ranked:
            hpc = holds_per_copy(r)
            held = db.hot_hold(conn, slug, r["system"], r["bib_id"])
            mark = {"placed": " ★HELD",
                    "failed": " !failed"}.get(held["outcome"], "") if held else ""
            if not r["holdable"]:
                # San Jose's 28-copy Lucky Day shelf shows 1 hold on 28 copies
                # — a dazzling 0.04/copy queue that takes no holds at all. Say
                # so on the row, or the best-looking line is an unjoinable one.
                mark += " (no holds — walk-in only)"
            ratio = "         —" if hpc is None else f"{hpc:>5.2f}/copy"
            print(f"  {r['system']:<5} {(r['format'] or '?')[:12]:<12} "
                  f"copies {r['copies'] or 0:>4}  holds {r['holds'] or 0:>4}  "
                  f"{ratio}  avail {r['available'] or 0:>3}{mark}")
        best = next((r for r in ranked
                     if holds_per_copy(r) is not None and r["holdable"]), None)
        if best is not None:
            print(f"  → shortest joinable queue: {best['system']} "
                  f"({holds_per_copy(best)} holds/copy)")


# --- credentials ----------------------------------------------------------------

class CredentialError(RuntimeError):
    pass


def secret(system: str, field: str) -> str:
    """Read one credential out of the login keyring via libsecret.

    Nothing here ever prints, logs or persists the value — it goes straight
    from `secret-tool` into the request that needs it. Store them with:

        secret-tool store --label='shelfwalk sccl barcode' \\
            service shelfwalk system sccl field barcode
        secret-tool store --label='shelfwalk sccl pin' \\
            service shelfwalk system sccl field pin
    """
    try:
        res = subprocess.run(
            ["secret-tool", "lookup", "service", "shelfwalk",
             "system", system, "field", field],
            capture_output=True, timeout=20)
    except FileNotFoundError as exc:
        raise CredentialError(
            "secret-tool not installed (apt install libsecret-tools)") from exc
    if res.returncode != 0 or not res.stdout:
        raise CredentialError(
            f"no {field} stored for {system} — see `secret` in hotlist.py")
    return res.stdout.decode().strip()


def have_credentials(system: str) -> bool:
    try:
        secret(system, "barcode") and secret(system, "pin")
        return True
    except CredentialError:
        return False


# --- placing holds --------------------------------------------------------------

class HoldPlacer:
    """One system's authenticated hold route.

    Split out behind an interface because the two catalog platforms need
    genuinely different work, and neither endpoint has been captured yet — see
    `docs/hold-recon.md` for what to record from a logged-in session.
    """

    system = "?"

    def place(self, bib_id: str, pickup: str = None) -> str:
        raise NotImplementedError


class BiblioCommonsHolds(HoldPlacer):
    """SCCL and San Jose. Needs the authenticated session + CSRF token that
    the public gateway API doesn't use."""

    def __init__(self, subdomain: str):
        self.system = subdomain

    def place(self, bib_id: str, pickup: str = None) -> str:
        raise NotImplementedError(
            f"{self.system}: hold endpoint not captured yet — the gateway API "
            "this repo uses is read-only and unauthenticated. Capture the "
            "logged-in POST first (see docs/hold-recon.md).")


class WebPacHolds(HoldPlacer):
    """Mountain View / LINK+ classic Innovative. Historically a form POST of
    name + barcode + PIN to /search~S1/.b<id>/.b<id>/1,1,1,B/request, but the
    field names differ per install and must be captured, not guessed."""

    system = "mvpl"

    def place(self, bib_id: str, pickup: str = None) -> str:
        raise NotImplementedError(
            "mvpl: request-form fields not captured yet "
            "(see docs/hold-recon.md).")


PLACERS = {
    "sccl": lambda: BiblioCommonsHolds("sccl"),
    "sjpl": lambda: BiblioCommonsHolds("sjpl"),
    "mvpl": WebPacHolds,
}


def plan_holds(db_path: str, watchlist: str = WATCHLIST_FILE) -> list[dict]:
    """Decide, for each watched title, the one bib worth queueing on.

    One hold per title, not one per system: the point is to read the book, and
    a duplicate hold at a second system just takes a copy out of circulation
    for someone else and leaves you cancelling it later.
    """
    conn = db.open_db(db_path)
    entries = {e["slug"]: e for e in load_watchlist(watchlist)}
    plans = []
    for slug, entry in entries.items():
        if not entry["auto_hold"]:
            continue
        rows = db.latest_hot_sightings(conn, slug)
        if any(h["outcome"] == "placed" for h in db.hot_holds_for(conn, slug)):
            continue                       # already queued for this title
        eligible = []
        for r in rows:
            if not r["holdable"] or r["format_class"] not in entry["formats"]:
                continue
            hpc = holds_per_copy(r)
            if hpc is None or hpc > entry["max_holds_per_copy"]:
                continue
            if not have_credentials(r["system"]):
                continue
            eligible.append((hpc, r))
        if not eligible:
            continue
        hpc, row = min(eligible, key=lambda t: t[0])
        plans.append({
            "slug": slug, "title": entry["title"], "system": row["system"],
            "bib_id": row["bib_id"], "holds_per_copy": hpc,
            "holds": row["holds"], "copies": row["copies"],
            "pickup": entry["pickup"].get(row["system"]),
        })
    return plans


def run_holds(db_path: str, watchlist: str = WATCHLIST_FILE,
              place: bool = False, limit: int = 5) -> list[dict]:
    conn = db.open_db(db_path)
    plans = plan_holds(db_path, watchlist)
    if not plans:
        print("nothing to hold")
        return []
    if len(plans) > limit:
        print(f"! {len(plans)} holds planned but the cap is {limit} — "
              f"refusing to place any. Raise --limit deliberately.")
        return plans
    for p in plans:
        line = (f"{p['title']} → {p['system']} {p['bib_id']} "
                f"({p['holds']} holds / {p['copies']} copies, "
                f"{p['holds_per_copy']}/copy)"
                + (f", pickup {p['pickup']}" if p['pickup'] else ""))
        if not place:
            print(f"DRY RUN  {line}")
            continue
        try:
            detail = PLACERS[p["system"]]().place(p["bib_id"], p["pickup"])
            outcome = "placed"
        except Exception as exc:                      # noqa: BLE001
            detail, outcome = f"{type(exc).__name__}: {exc}", "failed"
        print(f"{outcome.upper():<8} {line}\n         {detail}")
        db.record_hot_hold(conn, p["slug"], p["system"], p["bib_id"], outcome,
                           pickup=p["pickup"], detail=detail,
                           holds=p["holds"], copies=p["copies"])
    return plans


# --- cli ------------------------------------------------------------------------

def cmd_add(args) -> None:
    entries = load_watchlist(args.watchlist)
    slug = slugify(args.title)
    if any(e["slug"] == slug for e in entries):
        print(f"already watching {slug}")
        return
    entries.append({
        "slug": slug, "title": args.title, "author": args.author,
        "isbns": [_norm_isbn(i) for i in (args.isbn or [])],
        "pub_date": args.pub_date, "systems": WATCH_SYSTEMS,
        "formats": DEFAULT_FORMATS, "auto_hold": args.auto_hold,
        "pickup": {}, "max_holds_per_copy": DEFAULT_MAX_HOLDS_PER_COPY,
    })
    save_watchlist(entries, args.watchlist)
    print(f"watching {slug}"
          + ("" if args.isbn else "  (no ISBN — matching by title, which is "
                                  "what mis-catalogued pre-pub records break)"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="shelfwalk.db")
    ap.add_argument("--watchlist", default=WATCHLIST_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="poll every watched title")
    c.add_argument("--quiet", action="store_true",
                   help="print nothing unless something moved")
    c.add_argument("--only", help="just the slugs containing this substring")

    sub.add_parser("status", help="current standing per title")

    h = sub.add_parser("holds", help="show or place the planned holds")
    h.add_argument("--place", action="store_true",
                   help="actually place them (default is a dry run)")
    h.add_argument("--limit", type=int, default=5,
                   help="refuse the whole batch above this many (default 5)")

    a = sub.add_parser("add", help="add a title to the watchlist")
    a.add_argument("title")
    a.add_argument("--author")
    a.add_argument("--isbn", action="append")
    a.add_argument("--pub-date")
    a.add_argument("--auto-hold", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "check":
        evs = check(args.db, args.watchlist, quiet=args.quiet, only=args.only)
        return 0 if not evs else 10       # 10 = "something moved", for the timer
    if args.cmd == "status":
        status(args.db, args.watchlist)
    elif args.cmd == "holds":
        run_holds(args.db, args.watchlist, place=args.place, limit=args.limit)
    elif args.cmd == "add":
        cmd_add(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
