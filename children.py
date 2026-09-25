#!/usr/bin/env python3
"""Download children's award bibliographies, then search them without API calls.

    uv run children.py pull                 # mirror-first; saves JSON/CSV + sources
    uv run children.py pull --refresh       # deliberately refresh official archives
    uv run children.py find --age 3 --new-only
    uv run children.py find --award geisel
    uv run children.py find "truck"
    uv run children.py report

The archive covers all childhood ages. Age filters select explicitly reviewed
read-aloud recommendations, not every winner of a children's award.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import catalog_db as db
from acclaim_core import _fetch
from sources import children_awards as C

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "children"
ALA = "https://www.ala.org/alsc/awardsgrants/bookmedia/"
LABELS = {"caldecott": "Caldecott Medal", "geisel": "Theodor Seuss Geisel Award",
          "newbery": "Newbery Medal", "sibert": "Sibert Informational Book Medal",
          "belpre": "Pura Belpré Award", "batchelder": "Batchelder Award",
          "ejk": "Ezra Jack Keats Award", "carnegie": "Carnegie Medal for Writing",
          "greenaway": "Kate Greenaway / Carnegie Medal for Illustration",
          "csk": "Coretta Scott King Book Awards",
          "mwb-board": "Margaret Wise Brown Board Book Award",
          "zolotow": "Charlotte Zolotow Award", "hornbook": "Boston Globe–Horn Book Award"}
EJK = "https://www.ejkf.org/ejk-award-all-winners-and-honorees/"
CARNEGIE = "https://carnegies.co.uk/archive/"
ZOLOTOW = "https://ccbc.education.wisc.edu/booklists/?booklistId=3"
HORNBOOK = "https://www.hbook.com/page/past-boston-globe-horn-book-award-winners"
CSK = "https://www.ala.org/awards/books-media/coretta-scott-king-book-awards"
BOARD = "https://www.bankstreet.edu/library/center-for-childrens-literature/the-margaret-wise-brown-board-book-award/"


def norm(title):
    import unicodedata
    title = unicodedata.normalize("NFKD", title).casefold()
    return re.sub(r"[^\w]", "", "".join(c for c in title if not unicodedata.combining(c)))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Archive:
    def __init__(self, conn, directory=DATA, refresh=False):
        self.conn, self.directory, self.refresh = conn, Path(directory), refresh
        self.pages, self.errors = {}, []
        self.directory.joinpath("raw").mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "manifest.json"
        self.saved_pages = {p["url"]: p for p in json.loads(manifest.read_text())["pages"]} if manifest.exists() else {}

    def get(self, url):
        if url in self.pages:
            return (self.directory / self.pages[url]["file"]).read_bytes()
        saved = self.saved_pages.get(url) if not self.refresh else None
        if saved and (self.directory / saved["file"]).exists():
            raw = (self.directory / saved["file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != saved["sha256"]:
                raise ValueError(f"Saved source checksum mismatch: {url}")
            self.pages[url] = saved
            return raw
        raw = _fetch(self.conn, url, self.errors, fresh=self.refresh, timeout=30)
        if raw is None:
            raise RuntimeError(self.errors[-1])
        suffix = ".pdf" if raw.startswith(b"%PDF") else ".html"
        # Content-addressed bytes keep the previous manifest valid if refresh
        # fails after downloading a changed source.
        filename = "raw/" + hashlib.sha256(raw).hexdigest()[:20] + suffix
        (self.directory / filename).write_bytes(raw)
        ts = self.conn.execute("SELECT fetched_at FROM raw_pages WHERE url=?", (url,)).fetchone()
        self.pages[url] = dict(file=filename, sha256=hashlib.sha256(raw).hexdigest(),
                               fetched_at=ts[0] if ts else None, url=url)
        return raw


def validate(rows, source):
    if not rows:
        raise ValueError(f"{source}: parsed no entries")
    for row in rows:
        if not row.get("title") or len(row["title"]) > 240 or not 1900 <= row["year"] <= datetime.now().year + 1:
            raise ValueError(f"{source}: invalid bibliographic entry {row!r}")
        if row["status"] not in ("winner", "honor", "shortlist", "longlist", "finalist", "nominee", "listed", "commended"):
            raise ValueError(f"{source}: unknown status {row['status']}")


def pull(conn, directory=DATA, refresh=False):
    """Publish only a fully parsed run. Failed downloads retain the old export."""
    directory = Path(directory)
    archive = Archive(conn, directory, refresh)
    entries = []
    coverage = []

    def add(source, url, rows, note=""):
        validate(rows, source)
        for row in rows:
            entries.append(dict(row, source=source, award=LABELS.get(source, source),
                                source_url=url, year_basis=row.get("year_basis", "award")))
        coverage.append(dict(source=source, url=url, count=len(rows),
                             first_year=min(r["year"] for r in rows),
                             last_year=max(r["year"] for r in rows),
                             years=sorted({r["year"] for r in rows}),
                             statuses=sorted({r["status"] for r in rows}), note=note))

    for source in ("caldecott", "geisel", "newbery", "sibert", "belpre", "batchelder"):
        landing = ALA + source
        soup = BeautifulSoup(archive.get(landing), "html.parser")
        links = [urljoin(landing, a["href"]) for a in soup.select("a[href]")
                 if ".pdf" in a["href"].lower() and "complete" in a.get_text().lower()]
        if len(links) != 1:
            raise ValueError(f"{source}: expected one complete bibliography PDF, found {links}")
        add(source, links[0], C.parse_ala_pdf(archive.get(links[0]), source),
            "Official full-history winner/honor bibliography; private nominations are not published.")
        print(f"{source}: {coverage[-1]['count']} entries", flush=True)
    add("ejk", EJK, C.parse_ejk(archive.get(EJK)), "Writer and illustrator awards/honors.")
    for source, path in (("carnegie", "writing-winners/"), ("greenaway", "illustration-winners/")):
        url = CARNEGIE + path
        add(source, url, C.parse_carnegie_winners(archive.get(url), source == "greenaway"),
            "Full winner archive; before 2007 the source labels publication year, not ceremony year.")
    soup = BeautifulSoup(archive.get(CARNEGIE), "html.parser")
    links = sorted({a["href"] for a in soup.select("a[href]")
                    if re.search(r"/archive/(?:20\d\d|2010-2015)-shortlist-resources/", a["href"])})
    if not links:
        raise ValueError("Carnegie shortlist archive links missing")
    for url in links:
        rows = C.parse_carnegie_shortlists(archive.get(url))
        for category, source in (("Writing", "carnegie"), ("Illustration", "greenaway")):
            add(source, url, [r for r in rows if r["category"] == category],
                "Public shortlists, plus longlists where the archive includes them; pre-2010 shortlists unavailable here.")
    add("csk", CSK, C.parse_csk_table(archive.get(CSK)),
        "Legacy official table; author/illustrator category and credits absent. Coverage gaps listed by year.")
    for year in sorted(set(range(1970, datetime.now().year + 1)) - {r["year"] for r in entries if r["source"] == "csk"}):
        url = f"https://www.ala.org/cskbart/{year}-" + ("winners" if year == 2024 else "winners-and-honors")
        add("csk", url, C.parse_csk_year(archive.get(url), year), "Annual official page fills a gap in the legacy table; excludes lifetime achievement.")
    add("zolotow", ZOLOTOW, C.parse_zolotow(archive.get(ZOLOTOW)),
        "Complete archive; highly commended is separate from honor. Includes CCBC age guidance. 2021–2022 is a combined award cycle.")
    add("hornbook", HORNBOOK, C.parse_hornbook(archive.get(HORNBOOK)),
        "Official historical archive currently ends in 2025; 2026 not yet included here.")
    add("mwb-board", BOARD, C.parse_board_award(archive.get(BOARD)),
        "Current 2025 award cycle. Established 2023; earlier cycle recorded separately.")
    board2023 = "https://www.bankstreet.edu/news-events/news/bank-street-hosts-inaugural-margaret-wise-brown-board-book-award-ceremony-and-educational-program/"
    text = BeautifulSoup(archive.get(board2023), "html.parser").get_text(" ", strip=True)
    rows = []
    for title, author, illustrator, category in [
        ("Give Me a Snickle!", "Alisha Sevigny", None, "0–18 months"),
        ("Me and the Family Tree", "Carole Boston Weatherford", "Ashleigh Corrin", "19–36 months")]:
        if title not in text or author not in text:
            raise ValueError("2023 board award announcement changed")
        rows.append(dict(title=title, author=author, illustrator=illustrator, category=category, year=2023, status="winner"))
    add("mwb-board", board2023, rows, "Inaugural winners explicitly verified against the announcement; other recommended board books are not award nominees.")
    # Preserve the original Irma Black archive even where cover images have no
    # textual title. Do not manufacture book titles from image filenames.
    irma = "https://www.bankstreet.edu/library/center-for-childrens-literature/irma-black-award/past-winners/"
    archive.get(irma)
    coverage.append(dict(source="irma-black", count=0, url=irma,
                         note="Full historical HTML saved; many entries are only cover images without title text. Not normalized."))
    # Reuse already-downloaded children's categories in the general corpus.
    old_entries = json.loads((directory / "awards.json").read_text()) if (directory / "awards.json").exists() else []
    for source, pattern in (("nba", "%Young People%"), ("kirkus", "%Young Readers%"),
                            ("goodreads", "%Picture%")):
        rows = conn.execute("""SELECT w.title, w.author, a.category, a.year, a.status,
                               a.url AS source_url FROM accolades a JOIN works w USING(work_key)
                               WHERE a.source=? AND a.category LIKE ?""", (source, pattern)).fetchall()
        # A fresh clone still has the portable export, even without the user's
        # large general acclaim database. Preserve its downloaded categories.
        if not rows:
            rows = [r for r in old_entries if r["source"] == source]
        for row in rows:
            entries.append(dict(row, source=source, award=source, year_basis="award", illustrator=None))
        if rows:
            coverage.append(dict(source=source, count=len(rows), first_year=min(r["year"] for r in rows),
                                 last_year=max(r["year"] for r in rows),
                                 note="Existing local acclaim corpus; not re-fetched by this command. Goodreads is popular vote."))
    # Idempotent; keep separate award categories and shortlist/winner stages.
    unique = {}
    for row in entries:
        key = (row["source"], row["year"], row["year_basis"], row["category"], row["status"], norm(row["title"]))
        unique.setdefault(key, row)
    entries = sorted(unique.values(), key=lambda r: (r["source"], -r["year"], r["title"], r["status"]))
    previous = directory / "awards.json"
    if previous.exists():
        from collections import Counter
        old_counts = Counter(r["source"] for r in old_entries)
        new_counts = Counter(r["source"] for r in entries)
        for source, count in old_counts.items():
            if new_counts[source] < count * .9:
                raise ValueError(f"{source}: parsed archive lost more than 10% of its entries; previous export retained")
    manifest = dict(generated_at=datetime.now().isoformat(timespec="seconds"),
                    scope="Major US/UK children's book awards, all ages; not an exhaustive worldwide award registry.",
                    entries=len(entries), coverage=coverage, pages=list(archive.pages.values()),
                    gaps=["Private nomination ballots are not public: honors are not nominees.",
                          "Boston Globe–Horn Book 2026 is not yet in its historical archive; Irma Black image-only entries are preserved but not normalized.",
                          "Not yet covered comprehensively: CBCA, Governor General’s, Asian/Pacific American, American Indian Youth Literature, Schneider, Stonewall, and other regional/international awards."])
    # Commit the complete normalized snapshot together. JSON/CSV are portable
    # exports; no award entries become availability want-list titles here.
    with conn:
        conn.execute("INSERT OR REPLACE INTO children_award_archive VALUES (1, ?, ?)",
                     (json.dumps(entries, ensure_ascii=False), json.dumps(manifest, ensure_ascii=False)))
    atomic_json(directory / "awards.json", entries)
    atomic_json(directory / "manifest.json", manifest)
    fields = ["source", "award", "year", "year_basis", "category", "status", "title", "author", "illustrator", "source_url"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(entries)
    (directory / "awards.csv").write_text(buf.getvalue(), encoding="utf-8")
    print(f"Saved {len(entries)} award entries to {directory}")
    return entries


def tracked_titles(conn):
    # Exact normalized work titles, plus the catalog's parallel-title variant.
    return {norm(re.split(r"\s*[=:]\s*", r[0])[0]) for r in conn.execute("SELECT title FROM titles")}


def same_book(recommendation, award):
    titles = {norm(recommendation["title"]), *map(norm, recommendation.get("award_titles", []))}
    if norm(award["title"]) not in titles:
        return False

    def name_key(name):
        if "," in name:
            last, first = name.split(",", 1)
            name = first + " " + last
        return norm(name)

    author = award.get("author") or award.get("illustrator")
    return bool(author and recommendation.get("author")
                and name_key(author) == name_key(recommendation["author"]))


def popular_acclaim(book, snapshots, awards):
    """Explain a small editorial ranking boost; never infer age fit or sales.

    Count each signal family once, even across editions and award years. Ratings
    are adult reader opinions; a nomination is weaker evidence than a win.
    """
    evidence = []
    points = {}
    for snapshot in snapshots:
        if not same_book(book, snapshot):
            continue
        for item in snapshot["evidence"]:
            item = dict(item)
            kind = item["kind"]
            if kind == "bestseller":
                score = 2
                item["summary"] = f"{item['list']} bestseller (historical publisher claim; checked {item['checked_at']})"
            elif kind == "reader-rating":
                score = 2 if item["rating"] >= 4.0 and item["rating_count"] >= 1000 else 0
                item["summary"] = (f"{item['source']}: {item['rating']:.2f}/5 from {item['rating_count']:,} ratings "
                                   f"(snapshot checked {item['checked_at']})")
            else:
                raise ValueError(f"Unknown popularity evidence kind: {kind}")
            points[kind] = max(points.get(kind, 0), score)
            evidence.append(item)
    for item in awards:
        if item["source"] != "goodreads" or not same_book(book, item):
            continue
        score = 2 if item["status"] == "winner" else 1
        points["readers-choice"] = max(points.get("readers-choice", 0), score)
        evidence.append(dict(kind="readers-choice", source_url=item["source_url"],
                             year=item["year"], status=item["status"],
                             summary=f"Goodreads Choice {item['year']} {item['status']} (reader-voted award)"))
    return dict(score=sum(points.values()), evidence=evidence,
                coverage="evidence saved" if evidence else "not assessed; no saved popularity evidence")


def reader_affinity(book, preferences):
    favorites = preferences.get("theme_connections", {}).get(book.get("theme"), [])
    return dict(score=1 if favorites else 0,
                favorites=favorites,
                reason=(f"Possible {book['theme']} connection to " + ", ".join(favorites)
                        if favorites else "No assessed connection to saved favorites"))


def find(directory=DATA, *, query="", award=None, age=None, candidates=False, new_only=False, popular=False, conn=None):
    directory = Path(directory)
    rows = json.loads((directory / "awards.json").read_text())
    all_awards = rows
    popularity_path = directory / "popularity.json"
    snapshots = json.loads(popularity_path.read_text()) if popularity_path.exists() else []
    preferences_path = directory / "preferences.json"
    preferences = json.loads(preferences_path.read_text()) if preferences_path.exists() else {}
    assessments = json.loads((directory / "recommendations.json").read_text()) if (directory / "recommendations.json").exists() else []
    if candidates:
        if age is None:
            raise ValueError("--candidates requires --age")
        rows = [r for r in rows if r.get("source_min_age") is not None
                and r["source_min_age"] <= age <= r["source_max_age"]]
    elif age is not None:
        rows = [dict(r, accolades=[a for a in rows if same_book(r, a)])
                for r in assessments if r["min_age"] <= age <= r["max_age"] and r["language"] not in ("fre", "spa", "jpn")]
    if award:
        rows = [r for r in rows if r.get("source") == award or any(a["source"] == award for a in r.get("accolades", []))]
    if query:
        rows = [r for r in rows if query.casefold() in " ".join(str(r.get(k) or "") for k in ("title", "author", "reason", "category")).casefold()]
    if new_only:
        if conn is None:
            raise ValueError("--new-only needs the local catalog database")
        tracked = tracked_titles(conn)
        tracked.update(norm(t) for t in preferences.get("favorites", []))
        rows = [r for r in rows if norm(re.split(r"\s*[=:]\s*", r["title"])[0]) not in tracked]
    rows = [dict(r, popularity=popular_acclaim(r, snapshots, all_awards),
                 affinity=reader_affinity(r, preferences)) for r in rows]
    if popular:
        rows = [r for r in rows if r["popularity"]["score"] > 0]
    # Keep age-fit/support groups ahead of popularity; stable ties preserve the
    # curated order. Unknown evidence stays eligible unless --popular is used.
    priorities = {"start-here": 0, "around-three": 1, "try-with-support": 2}
    rows.sort(key=lambda r: (priorities.get(r.get("priority"), 0),
                             -(r["popularity"]["score"] + r["affinity"]["score"])))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="shelfwalk.db")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--refresh", action="store_true")
    f = sub.add_parser("find")
    f.add_argument("query", nargs="?", default="")
    f.add_argument("--age", type=float)
    f.add_argument("--candidates", action="store_true", help="explore unreviewed entries with explicit source age guidance")
    f.add_argument("--award")
    f.add_argument("--new-only", action="store_true")
    f.add_argument("--popular", action="store_true", help="require saved bestseller, substantial positive ratings, or reader-choice evidence")
    f.add_argument("--json", action="store_true")
    sub.add_parser("report")
    args = parser.parse_args(argv)
    conn = db.open_db(args.db)
    try:
        if args.command == "pull":
            pull(conn, args.data_dir, args.refresh)
        elif args.command == "find":
            rows = find(args.data_dir, query=args.query, age=args.age, award=args.award,
                        candidates=args.candidates, new_only=args.new_only, popular=args.popular, conn=conn)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for r in rows:
                    print(f"{r['title']} — {r.get('author') or r.get('illustrator') or 'credit not supplied'}")
                    print("  " + (r.get("reason") or f"{r['source']} {r['year']} {r['status']}"))
                    print("  Popular acclaim: " + ("; ".join(e["summary"] for e in r["popularity"]["evidence"])
                                                   or r["popularity"]["coverage"]))
                    if r["affinity"]["score"]:
                        print("  Personal fit: " + r["affinity"]["reason"])
                label = "source age guidance; not individually reviewed" if args.candidates else "reviewed read-aloud suggestions" if args.age is not None else "award records, all childhood ages"
                print(f"{len(rows)} results; {label}.")
        else:
            import report
            print(report.write_children(args.db, args.data_dir))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
