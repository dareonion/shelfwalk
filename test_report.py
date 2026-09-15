"""Tests for report.py (markdown generation) — seeds a temp DB, no browser."""
from __future__ import annotations

import os
import tempfile

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

        assert set(report.stale_systems(dbp)) == {"mvpl"}
        report.write_bayarea(dbp, d)
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
