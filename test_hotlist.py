"""Tests for hotlist.py — the hot-release watcher. No network, no browser.

Every case below is a bug that either bit during the Taipei Story build or is
the specific thing that would make the machinery place a wrong hold.
"""
from __future__ import annotations

import json
import os
import tempfile

import catalog_db as db
import hotlist as H


# --- matching -------------------------------------------------------------------

def test_isbn_beats_a_mangled_title():
    """The bug that started this: San Jose catalogued Taipei Story as 'Taipei
    Story (Deluxe Limited Edition)', which the want-list matcher scored 0.667
    and discarded — losing the one system whose queue was worth joining."""
    entry = {"title": "Taipei Story", "author": "Kuang",
             "isbns": ["9780063473744"]}
    cand = {"title": "Taipei Story (Deluxe Limited Edition)",
            "isbns": ["9780063473744"], "authors": ["Kuang, R F"]}
    assert H._entry_matches(entry, cand)


def test_title_and_author_match_without_isbn():
    entry = {"title": "Taipei Story", "author": "Kuang", "isbns": []}
    assert H._entry_matches(
        entry, {"title": "Taipei story : a novel", "isbns": [],
                "authors": ["Kuang, R. F."]})


def test_wrong_author_is_rejected():
    entry = {"title": "Taipei Story", "author": "Kuang", "isbns": []}
    assert not H._entry_matches(
        entry, {"title": "Taipei Story", "isbns": [],
                "authors": ["Yang, Edward"]})


def test_unrelated_title_is_rejected():
    entry = {"title": "Taipei Story", "author": "Kuang", "isbns": []}
    assert not H._entry_matches(
        entry, {"title": "Made in Taiwan : recipes", "isbns": [],
                "authors": ["Kuang, R. F."]})


def test_isbn_normalisation_ignores_hyphens():
    entry = {"title": "X", "author": None, "isbns": ["9780063473744"]}
    cand = {"title": "Anything", "isbns": [H._norm_isbn("978-0-06-347374-4")],
            "authors": []}
    assert H._entry_matches(entry, cand)


# --- the WebPAC hold-queue line -------------------------------------------------

def test_webpac_holds_line():
    page = "<div>12 holds on first copy returned of 3 copies</div>"
    assert H.webpac_holds(page) == (12, 3)


def test_webpac_holds_single_copy_phrasing():
    """A one-copy record drops the 'first copy returned of' clause."""
    assert H.webpac_holds("<p>1 hold on 1 copy</p>") == (1, 1)


def test_webpac_holds_absent_is_none():
    assert H.webpac_holds("<p>nothing here</p>") == (None, None)


# --- ratios and labels ----------------------------------------------------------

def test_holds_per_copy_handles_zero_copies():
    assert H.holds_per_copy({"holds": 5, "copies": 0}) is None
    assert H.holds_per_copy({"holds": None, "copies": 3}) is None
    assert H.holds_per_copy({"holds": 60, "copies": 38}) == 1.58


def test_format_label_drops_a_physical_description():
    assert H._format_label("279 pages : illustrations ; 24 cm", "book") == "Book"
    assert H._format_label("Adult Fiction Book", "book") == "Adult Fiction Book"


# --- transitions ----------------------------------------------------------------

def test_new_record_is_the_headline_event():
    assert H._transitions(None, {"holdable": True}) == ["NEW RECORD"]


def test_holds_movement_is_reported_with_the_ratio():
    prev = {"holdable": 1, "available": 0, "holds": 18, "copies": 50}
    evs = H._transitions(prev, {"holdable": True, "available": 0,
                                "holds": 25, "copies": 50})
    assert any("18→25" in e and "0.5/copy" in e for e in evs)


def test_no_change_is_no_event():
    prev = {"holdable": 1, "available": 0, "holds": 18, "copies": 50}
    assert H._transitions(prev, {"holdable": True, "available": 0,
                                 "holds": 18, "copies": 50}) == []


# --- the hold ledger ------------------------------------------------------------

def _seeded(d, *, holdable=True, holds=18, copies=50, system="sjpl"):
    """A watchlist of one, sighted once, with auto_hold on."""
    dbp = os.path.join(d, "t.db")
    wl = os.path.join(d, "hotlist.json")
    with open(wl, "w") as fh:
        json.dump([{"title": "Taipei Story", "author": "Kuang",
                    "isbns": ["9780063473744"], "auto_hold": True,
                    "pickup": {"sjpl": "Calabazas"}}], fh)
    conn = db.open_db(dbp)
    db.sync_hotlist(conn, H.load_watchlist(wl))
    db.add_hot_sighting(conn, "taipei-story", system, {
        "bib_id": "S156C6810789", "title": "Taipei Story", "format": "BK",
        "format_class": "book", "holdable": holdable, "status": "UNAVAILABLE",
        "copies": copies, "on_order": 0, "available": 0, "holds": holds,
    }, "2026-09-05T02:00:00")
    conn.commit()
    return dbp, wl, conn


def test_placed_hold_is_never_placed_twice():
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        assert db.record_hot_hold(conn, "taipei-story", "sjpl",
                                  "S156C6810789", "placed")
        # a second attempt on the same bib must not write a new row
        assert not db.record_hot_hold(conn, "taipei-story", "sjpl",
                                      "S156C6810789", "placed")
        rows = db.hot_holds_for(conn, "taipei-story")
        assert len(rows) == 1


def test_a_failed_attempt_can_be_retried():
    """A transient login error must not lock a title out of its queue."""
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        db.record_hot_hold(conn, "taipei-story", "sjpl", "S156C6810789",
                           "failed", detail="timeout")
        assert db.record_hot_hold(conn, "taipei-story", "sjpl",
                                  "S156C6810789", "placed")
        rows = db.hot_holds_for(conn, "taipei-story")
        assert len(rows) == 1 and rows[0]["outcome"] == "placed"


def test_plan_skips_a_title_already_held(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        assert len(H.plan_holds(dbp, wl)) == 1
        db.record_hot_hold(conn, "taipei-story", "sjpl", "S156C6810789",
                           "placed")
        assert H.plan_holds(dbp, wl) == []


def test_plan_never_targets_a_no_holds_record(monkeypatch):
    """San Jose's Lucky Day shelf reads 1 hold on 28 copies — a 0.04/copy queue
    that accepts no holds at all. Ranking on the ratio alone picks it."""
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d, holdable=False, holds=1, copies=28)
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        assert H.plan_holds(dbp, wl) == []


def test_plan_skips_a_system_with_no_card(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        monkeypatch.setattr(H, "have_credentials", lambda s: False)
        assert H.plan_holds(dbp, wl) == []


def test_plan_refuses_a_hopeless_queue(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d, holds=120, copies=3)   # 40 holds/copy
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        assert H.plan_holds(dbp, wl) == []


def test_plan_picks_the_shortest_joinable_queue(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)                        # sjpl 18/50 = 0.36
        db.add_hot_sighting(conn, "taipei-story", "sccl", {
            "bib_id": "S118C1339840", "title": "Taipei Story", "format": "BK",
            "format_class": "book", "holdable": True, "status": "UNAVAILABLE",
            "copies": 38, "on_order": 30, "available": 0, "holds": 60,
        }, "2026-09-05T02:00:00")
        conn.commit()
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        plans = H.plan_holds(dbp, wl)
        assert len(plans) == 1
        assert plans[0]["system"] == "sjpl" and plans[0]["pickup"] == "Calabazas"


def test_dry_run_writes_no_ledger_row(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        H.run_holds(dbp, wl, place=False)
        assert "DRY RUN" in capsys.readouterr().out
        assert db.hot_holds_for(conn, "taipei-story") == []


def test_batch_cap_refuses_the_whole_run(monkeypatch, capsys):
    """A matcher bug that suddenly plans twenty holds must place none."""
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        monkeypatch.setattr(H, "plan_holds",
                            lambda *a, **k: [{"slug": f"s{i}", "title": "t",
                                              "system": "sjpl", "bib_id": f"b{i}",
                                              "holds_per_copy": 0.1, "holds": 1,
                                              "copies": 10, "pickup": None}
                                             for i in range(20)])
        H.run_holds(dbp, wl, place=True, limit=5)
        assert "refusing to place any" in capsys.readouterr().out
        assert db.hot_holds_for(conn) == []


def test_unimplemented_placer_records_failure_not_success(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        monkeypatch.setattr(H, "have_credentials", lambda s: True)
        H.run_holds(dbp, wl, place=True)
        assert "FAILED" in capsys.readouterr().out
        rows = db.hot_holds_for(conn, "taipei-story")
        assert len(rows) == 1 and rows[0]["outcome"] == "failed"


# --- watchlist round-trip -------------------------------------------------------

def test_watchlist_round_trips_without_growing_defaults():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "hotlist.json")
        with open(p, "w") as fh:
            json.dump([{"title": "Taipei Story", "author": "Kuang",
                        "isbns": ["9780063473744"], "auto_hold": True}], fh)
        H.save_watchlist(H.load_watchlist(p), p)
        with open(p) as fh:
            again = json.load(fh)
        assert again == [{"title": "Taipei Story", "author": "Kuang",
                          "isbns": ["9780063473744"], "auto_hold": True}]


def test_retiring_a_title_keeps_its_history():
    with tempfile.TemporaryDirectory() as d:
        dbp, wl, conn = _seeded(d)
        db.sync_hotlist(conn, [])
        row = conn.execute(
            "SELECT retired_at FROM hot_titles WHERE slug = 'taipei-story'"
        ).fetchone()
        assert row["retired_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) c FROM hot_sightings").fetchone()["c"] == 1
