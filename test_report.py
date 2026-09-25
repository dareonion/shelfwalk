"""Tests for report.py (markdown generation) — seeds a temp DB, no browser."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import catalog_db as db
import report


def _seed(db_path):
    conn = db.open_db(db_path)
    ts = "2026-07-26T12:00:00"
    db.upsert_title(conn, "SD_ILS:1", "Yue liang wan an (Goodnight Moon, Chinese)", ts)
    db.upsert_title(conn, "SD_ILS:2", "Kitten's first full moon", ts)
    s = db.record_scrape(conn, "detail", ts, source="test")
    db.add_availability(conn, s, "SD_ILS:1", "Yue liang wan an (Goodnight Moon, Chinese)",
                        [{"branch": "North", "call_number": "JP BRO",
                          "status": "Juvenile World Language Collection"}], ts)
    # two copies at North: one out, one available -> should collapse to one "on shelf"
    db.add_availability(conn, s, "SD_ILS:2", "Kitten's first full moon",
                        [{"branch": "North", "call_number": "JP HEN", "status": "Checked Out"},
                         {"branch": "North", "call_number": "JP HEN", "status": "Juvenile Picture Book"}], ts)
    conn.commit()
    conn.close()


def test_write_all_creates_files_from_db():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)
        written = report.write_all(dbp, outdir=d)
        names = {os.path.basename(p) for p in written}
        assert names == {"books.md", "north.md", "lakeview.md", "main.md"}

        north = open(os.path.join(d, "north.md"), encoding="utf-8").read()
        # Chinese title routed to the World Language section
        assert "## Chinese / World Language" in north
        assert "Yue liang wan an" in north
        # multi-copy title appears exactly once, and counts as on-shelf (one copy available)
        assert north.count("Kitten's first full moon") == 1
        assert "**2 titles on the shelf**" in north

        books = open(os.path.join(d, "books.md"), encoding="utf-8").read()
        assert "AUTO-GENERATED" in books
        assert "Full availability matrix" in books
        assert "| North |" in books


def test_ages_reads_every_audience_shape():
    ages = lambda *v: report._ages({"audience": list(v)})          # noqa: E731
    # an age range wins, in any of the forms the catalogs actually store
    assert ages("Ages 3-7") == "Ages 3-7"
    assert ages("Ages 0-3. Random House Children's Books") == "Ages 0-3"
    assert ages("2-5 Brodart") == "Ages 2-5"
    assert ages("04-06") == "Ages 4-6"
    assert ages("3-7 years") == "Ages 3-7"
    assert ages("Ages 2+") == "Ages 2+"
    # bands and levels, in priority order
    assert ages("Pre-K to 1") == "PreK"
    assert ages("P-01") == "PreK"
    assert ages("Preschool") == "PreK"
    assert ages("Grades K - 3") == "Gr K-3"
    assert ages("K-3 Medialog, Inc") == "Gr K-3"
    assert ages("AD 280 Lexile") == "AD280L"
    assert ages("AD420L Lexile") == "AD420L"
    assert ages("120 Lexile") == "120L"
    assert ages("4-8", "K-3 Medialog, Inc", "190 Lexile") == "Ages 4-8"
    # reading-program numbers are not ages, and unusable values yield nothing
    assert ages("Accelerated Reader AR LG 2.0 0.5 87616") == ""
    assert ages("Reading Counts RC K-2 1.5 1 Quiz: 00584") == ""
    assert ages("Guided reading level: I") == ""
    assert ages("NP Lexile") == ""
    assert ages("-3.") == ""
    assert report._ages({}) == ""


def test_peoria_records_are_linked():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)
        report.write_all(dbp, outdir=d)
        books = open(os.path.join(d, "books.md"), encoding="utf-8").read()
        assert ("detailnonmodal/ent:$002f$002fSD_ILS$002f0$002fSD_ILS:2/one"
                in books)
        north = open(os.path.join(d, "north.md"), encoding="utf-8").read()
        assert "[Kitten's first full moon](https://alsi.sdp.sirsi.net" in north


def test_lakeview_empty_is_graceful():
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        _seed(dbp)  # nothing at Lakeview
        report.write_all(dbp, outdir=d)
        lake = open(os.path.join(d, "lakeview.md"), encoding="utf-8").read()
        assert "(none right now)" in lake


def test_a_system_left_out_of_the_refresh_shows_no_shelf_state():
    """A system the latest refresh left out gets a warning and '?' marks instead
    of its old shelf state, which no longer takes a title off the hold
    list."""
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "t.db")
        conn = db.open_db(dbp)
        old, new = "2026-09-11T07:30:00", "2026-09-14T07:30:00"
        db.upsert_title(conn, "WANT:1", "Dear zoo", old, {"format": "picture"})
        for system, ts, bib, branch in (
                ("sccl", new, "S1", "Milpitas Library"),
                ("mvpl", old, "b1", "Children's Picture Books")):
            sid = db.record_scrape(conn, "remote", ts, source="test", profile=system)
            db.upsert_remote_bib(conn, system, "WANT:1", ts,
                                 {"bib_id": bib, "title": "Dear zoo"}, 1.0)
            db.replace_remote_editions(conn, system, "WANT:1",
                                       [{"bib_id": bib, "title": "Dear zoo",
                                         "format_class": "picture",
                                         "kind": "primary"}], ts)
            db.add_remote_availability(conn, sid, system, "WANT:1", bib, "Dear zoo",
                                       [{"branch": branch, "call_number": "JP",
                                         "status": "AVAILABLE",
                                         "state": "available"}], ts)
        conn.commit()
        conn.close()

        assert set(report.stale_systems(dbp, datetime.fromisoformat(new))) == {"mvpl"}
        report.write_bayarea(dbp, d, now=datetime.fromisoformat(new))
        overview = open(os.path.join(d, "bayarea.md"), encoding="utf-8").read()
        assert "⚠ **Mountain View Public Library** was last checked **2026-09-11**" \
            in overview
        assert "| `mvpl` | Mountain View Public Library | 1 | ? (last checked " \
               "2026-09-11) |" in overview
        # Mountain View was a favourite shelf holding it; that must not count,
        # and Milpitas isn't a favourite, so the title needs a hold
        assert "### Place a hold" in overview
        assert "on the shelf at Milpitas Library" in overview

        mv = open(os.path.join(d, "mountainview.md"), encoding="utf-8").read()
        assert "want-list in the catalog" in mv
        assert "data as of **2026-09-11T07:30:00**" in mv
        assert "on the shelf" not in mv
        assert "[Dear zoo](https://classiccatalog.mountainview.gov/record=b1)" in mv
        sccl = open(os.path.join(d, "sccl.md"), encoding="utf-8").read()
        assert "Milpitas Library — 1 on the shelf" in sccl


def _remote(conn, system, rid, ts, branch, bib="b1"):
    db.upsert_title(conn, rid, rid, ts, {"format": "picture"})
    sid = db.record_scrape(conn, "remote", ts, profile=system)
    db.upsert_remote_bib(conn, system, rid, ts, {"bib_id": bib, "title": rid})
    db.replace_remote_editions(conn, system, rid,
                               [{"bib_id": bib, "title": rid,
                                 "format_class": "picture", "kind": "primary"}], ts)
    db.add_remote_availability(conn, sid, system, rid, bib, rid,
                               [{"branch": branch, "collection": "Children's Picture Books",
                                 "call_number": "J P TEST", "status": "AVAILABLE",
                                 "state": "available"}], ts)


def test_all_systems_expire_against_wall_clock(tmp_path):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    old = "2026-09-01T10:00:00"
    for system in ("sccl", "sjpl", "linkplus"):
        _remote(conn, system, "Old book", old, "Cupertino Library")
    conn.commit()
    conn.close()
    now = datetime(2026, 9, 18, 10)
    assert set(report.stale_systems(path, now)) == {"sccl", "sjpl", "linkplus"}
    report.write_bayarea(path, tmp_path, now=now)
    for filename in ("bayarea.md", "sccl.md", "sjpl.md", "linkplus.md"):
        md = (tmp_path / filename).read_text()
        assert "[✓" not in md
        assert "| ✓" not in md
    assert "no current available copy confirmed" in (tmp_path / "linkplus.md").read_text()


def test_old_title_expires_even_when_another_title_is_fresh(tmp_path):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    _remote(conn, "sccl", "Old book", "2026-09-01T10:00:00", "Cupertino Library", "old")
    _remote(conn, "sccl", "Fresh book", "2026-09-18T10:00:00", "Cupertino Library", "fresh")
    conn.commit()
    conn.close()
    now = datetime(2026, 9, 18, 11)
    assert report.stale_systems(path, now) == {}
    report.write_bayarea(path, tmp_path, now=now)
    overview = (tmp_path / "bayarea.md").read_text()
    assert "[?](https://sccl.bibliocommons.com/v2/record/old)" in overview
    assert "[✓](https://sccl.bibliocommons.com/v2/record/fresh)" in overview
    assert "shelf status unknown or not current" in overview
    shelf = (tmp_path / "sccl.md").read_text().split("## In the catalog,")[0]
    assert "[Old book]" not in shelf
    assert "[Fresh book]" in shelf


def test_mountain_view_linkplus_has_provenance_partial_coverage_and_expiry(tmp_path):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    ts = "2026-09-18T10:00:00"
    _remote(conn, "linkplus", "Dear zoo", ts, "Mountain View Public", "union1")
    _remote(conn, "linkplus", "Other library only", ts, "Palo Alto Public", "union2")
    _remote(conn, "mvpl", "Dear zoo", "2026-09-01T10:00:00", "Children's", "local1")
    conn.commit()
    conn.close()
    now = datetime(2026, 9, 18, 11)
    report.write_bayarea(path, tmp_path, now=now)
    md = (tmp_path / "mountainview-linkplus.md").read_text()
    assert "Partial coverage" in md
    assert "Missing titles" in md
    assert "https://csul.iii.com/record=union1" in md
    assert "union2" not in md and "local1" not in md
    assert ts in md and "Children's Picture Books" in md and "J P TEST" in md
    overview = (tmp_path / "bayarea.md").read_text()
    assert "[✓](https://csul.iii.com/record=union1)" in overview
    # A fresh Mountain View-owned copy satisfies the local shelf ladder.
    todo = overview.split("## To do")[1].split("## Your branches")[0]
    assert "Dear zoo" not in todo
    assert "mountainview-linkplus.md" in (tmp_path / "mountainview.md").read_text()
    conn = db.open_db(path)
    assert conn.execute("SELECT count(*) FROM remote_bibs WHERE system='mvpl_linkplus'").fetchone()[0] == 0
    conn.close()
    report.write_bayarea(path, tmp_path, now=now + timedelta(days=1))
    md = (tmp_path / "mountainview-linkplus.md").read_text()
    assert "Historical: AVAILABLE" in md
    assert "— 1 on the shelf" not in md
    overview = (tmp_path / "bayarea.md").read_text()
    assert "[?](https://csul.iii.com/record=union1)" in overview
