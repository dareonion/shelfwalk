"""Offline archive, age selection, and failure behavior using saved sources."""
import hashlib
import json
import shutil

import pytest

import catalog_db as db
import children as C


def copy_archive(tmp_path):
    directory = tmp_path / "children"
    shutil.copytree(C.DATA, directory)
    return directory


def no_network(*args, **kwargs):
    pytest.fail("saved archive operation must not make a network request")


def test_portable_archive_rebuild_is_offline_and_preserves_local_corpus(tmp_path, monkeypatch):
    directory = copy_archive(tmp_path)
    conn = db.open_db(str(tmp_path / "empty.db"))
    monkeypatch.setattr(C, "_fetch", no_network)
    before = json.loads((directory / "awards.json").read_text())
    rows = C.pull(conn, directory)
    assert rows == before
    assert conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 0
    saved = conn.execute("SELECT entries_json FROM children_award_archive").fetchone()[0]
    assert json.loads(saved) == rows
    assert {r["source"] for r in rows} >= {"nba", "kirkus", "goodreads"}
    conn.close()


def test_changed_failed_refresh_retains_last_complete_snapshot(tmp_path, monkeypatch):
    directory = copy_archive(tmp_path)
    conn = db.open_db(str(tmp_path / "test.db"))
    C.pull(conn, directory)
    original = (directory / "awards.json").read_bytes()
    manifest = (directory / "manifest.json").read_bytes()
    snapshot = conn.execute("SELECT entries_json FROM children_award_archive").fetchone()[0]
    monkeypatch.setattr(C, "_fetch", lambda *args, **kwargs: b"<html>Changed or blocked page</html>")
    with pytest.raises(ValueError, match="complete bibliography"):
        C.pull(conn, directory, refresh=True)
    assert (directory / "awards.json").read_bytes() == original
    assert (directory / "manifest.json").read_bytes() == manifest
    assert conn.execute("SELECT entries_json FROM children_award_archive").fetchone()[0] == snapshot
    for p in json.loads(manifest)["pages"]:
        assert hashlib.sha256((directory / p["file"]).read_bytes()).hexdigest() == p["sha256"]
    conn.close()


def test_per_source_yield_guard_catches_partial_parse(tmp_path, monkeypatch):
    directory = copy_archive(tmp_path)
    conn = db.open_db(str(tmp_path / "test.db"))
    original = (directory / "awards.json").read_bytes()
    real_parser = C.C.parse_ejk
    monkeypatch.setattr(C.C, "parse_ejk", lambda raw: real_parser(raw)[:2])
    monkeypatch.setattr(C, "_fetch", no_network)
    with pytest.raises(ValueError, match="ejk: parsed archive lost"):
        C.pull(conn, directory)
    assert (directory / "awards.json").read_bytes() == original
    assert conn.execute("SELECT COUNT(*) FROM children_award_archive").fetchone()[0] == 0
    conn.close()


def test_age_selection_is_explicit_and_new_only_checks_existing_titles(monkeypatch):
    monkeypatch.setattr(C, "_fetch", no_network)
    conn = db.open_db(":memory:")
    db.upsert_title(conn, "existing", "First the Egg", "2026-09-18", {})
    toddler = C.find(age=2.5, new_only=True, conn=conn)
    assert "First the Egg" not in {r["title"] for r in toddler}
    assert "Towed by Toad" not in {r["title"] for r in toddler}
    assert "Towed by Toad" in {r["title"] for r in C.find(age=3)}
    assert all(r["language"] == "eng" and r["reason"] for r in toddler)
    geisel = C.find(age=3, award="geisel")
    assert "Towed by Toad" in {r["title"] for r in geisel}
    assert "Red Sled" not in {r["title"] for r in geisel}
    conn.close()


def test_candidates_require_source_age_guidance():
    candidates = C.find(age=2.5, candidates=True)
    assert candidates
    assert all(r["source_min_age"] <= 2.5 <= r["source_max_age"] for r in candidates)
    assert "Every Monday Mabel" in {r["title"] for r in candidates}
    assert "Our Lake" not in {r["title"] for r in candidates}
    with pytest.raises(ValueError, match="requires --age"):
        C.find(candidates=True)


def test_award_evidence_does_not_attach_to_another_authors_same_title():
    pick = dict(title="Waiting", author="Henkes, Kevin")
    assert C.same_book(pick, dict(title="Waiting", author="Kevin Henkes"))
    assert not C.same_book(pick, dict(title="Waiting", author="Another Writer"))
    assert not C.same_book(pick, dict(title="Waiting", author=None))


def test_archive_preserves_award_stages_and_year_basis():
    rows = json.loads((C.DATA / "awards.json").read_text())
    assert {r["status"] for r in rows} >= {"winner", "honor", "nominee", "shortlist", "longlist", "commended"}
    assert any(r["source"] == "greenaway" and r["year_basis"] == "publication" for r in rows)
    assert any(r.get("year_label") == "2021-2022" and r["year"] == 2022 for r in rows)
    assert all(r["source_url"].startswith("https://") for r in rows)


def test_children_report_discloses_scope_and_has_no_availability_claim(tmp_path):
    import report
    path = str(tmp_path / "test.db")
    db.open_db(path).close()
    report_path = report.write_children(path, outdir=tmp_path)
    text = open(report_path).read()
    assert "AUTO-GENERATED" in text and "Towed by Toad" in text
    assert "not an exhaustive worldwide" in text
    assert "no library polling load" in text
    assert "private" in text.lower()
    assert "Popular acclaim" in text and "snapshot checked" in text
    assert "historical publisher claim" in text and "Not assessed" in text


def test_popularity_preserves_age_fit_and_new_only_is_offline(monkeypatch):
    monkeypatch.setattr(C, "_fetch", no_network)
    rows = C.find(age=3, popular=True)
    titles = {r["title"] for r in rows}
    selected = {"A Ball for Daisy", "Hot Dog", "A Sick Day for Amos McGee", "Jabari Jumps", "We All Play"}
    assert selected <= titles
    assert "Hot Dog" not in {r["title"] for r in C.find(age=2.5, popular=True)}
    assert rows[-1]["priority"] == "try-with-support"
    assert all(r["popularity"]["score"] > 0 and r["popularity"]["evidence"] for r in rows)
    conn = db.open_db(":memory:")
    import bayarea_lookup as B
    B.sync_wantlist(conn, str(C.ROOT / "wantlist_en.json"))
    assert not selected & {r["title"] for r in C.find(age=3, popular=True, new_only=True, conn=conn)}
    conn.close()


def test_popularity_deduplicates_families_and_requires_substantial_ratings():
    snapshots = json.loads((C.DATA / "popularity.json").read_text())
    book = next(r for r in snapshots if r["title"] == "Jabari Jumps")
    awards = json.loads((C.DATA / "awards.json").read_text())
    baseline = C.popular_acclaim(book, snapshots, awards)
    assert baseline["score"] >= 2
    assert C.popular_acclaim(book, snapshots * 2, awards * 2)["score"] == baseline["score"]
    assert C.popular_acclaim(dict(book, author="Another Writer"), snapshots, awards)["score"] == 0
    assert C.popular_acclaim(dict(book, author=None), snapshots, awards)["evidence"] == []
    rating = book["evidence"][0]
    for changes in ({"rating_count": 999}, {"rating": 3.99}):
        weak = dict(book, evidence=[dict(rating, **changes)])
        result = C.popular_acclaim(book, [weak], [])
        assert result["score"] == 0 and result["evidence"]


def test_popularity_missing_snapshot_file_still_uses_awards(tmp_path, monkeypatch):
    directory = copy_archive(tmp_path)
    (directory / "popularity.json").unlink()
    monkeypatch.setattr(C, "_fetch", no_network)
    rows = C.find(directory, age=3, popular=True)
    assert rows and all(e["kind"] == "readers-choice" for r in rows for e in r["popularity"]["evidence"])
    unknown = next(r for r in C.find(directory, age=3) if r["title"] == "Red Sled")
    assert unknown["popularity"]["score"] == 0
    assert "not assessed" in unknown["popularity"]["coverage"]


def test_popularity_cli_json_has_provenance(tmp_path, capsys):
    C.main(["--db", str(tmp_path / "test.db"), "find", "--age", "3", "--popular", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows and all(e["source_url"].startswith("https://") for r in rows for e in r["popularity"]["evidence"])


def test_personal_affinity_is_separate_from_popularity_and_bounded():
    rows = C.find(age=3)
    by_title = {r["title"]: r for r in rows}
    assert by_title["A Ball for Daisy"]["affinity"]["score"] == 1
    assert "In My Heart" in by_title["A Ball for Daisy"]["affinity"]["favorites"]
    assert by_title["Red Sled"]["affinity"]["score"] == 1
    assert by_title["Red Sled"]["popularity"]["score"] == 0
    assert "Red Sled" not in {r["title"] for r in C.find(age=3, popular=True)}
    assert max(r["affinity"]["score"] for r in rows) == 1
    assert C.reader_affinity(by_title["A Ball for Daisy"], {})["score"] == 0


def test_new_only_omits_favorites_without_adding_them_to_wantlist(tmp_path):
    directory = copy_archive(tmp_path)
    preferences = json.loads((directory / "preferences.json").read_text())
    preferences["favorites"].append("Red Sled")
    C.atomic_json(directory / "preferences.json", preferences)
    conn = db.open_db(":memory:")
    assert "Red Sled" not in {r["title"] for r in C.find(directory, age=3, new_only=True, conn=conn)}
    assert conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("raw,title,author,illustrator", [
    ("Hattie Big Sky by Kirby Larson (Delacorte Press)", "Hattie Big Sky", "Kirby Larson", None),
    ("Is That the Bus? by Libby Koponen, illustrated by Katie Mazeika (Charlesbridge)",
     "Is That the Bus?", "Libby Koponen", "Katie Mazeika"),
    ("Hush! A Thai Lullaby by Minfong Ho, illustrated by Holly Meade", "Hush! A Thai Lullaby",
     "Minfong Ho", "Holly Meade"),
    ("Inch by Inch by Leo Lionni", "Inch by Inch", "Leo Lionni", None),
    ("Lampie written and illustratedby Annet Schaap and translated by Laura Watkinson "
     "(Pushkin Children’s Books)", "Lampie", "Annet Schaap", "Annet Schaap"),
    ("Sequoyah: The Cherokee Man Who Gave His People Writing by James Rumford, translated "
     "into Cherokee by Anna Sixkiller Huckaby (Houghton Mifflin Company)",
     "Sequoyah: The Cherokee Man Who Gave His People Writing", "James Rumford", None),
    ("Boxers & Saints written and illustrated by Gene Luen Yang, color by Lark Pien",
     "Boxers & Saints", "Gene Luen Yang", "Gene Luen Yang"),
])
def test_credit_splits_on_the_word_by_not_inside_names(raw, title, author, illustrator):
    """'by' inside a name (Kirby, Libby, Lullaby) is not a credit marker; a
    translator or colorist is never the author."""
    from sources.children_awards import credit
    got = credit(raw)
    assert (got["title"], got["author"], got["illustrator"]) == (title, author, illustrator)
