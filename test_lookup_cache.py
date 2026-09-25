"""Discovery reuse and request deduplication; all catalogs are simulated."""
from datetime import datetime, timedelta

import pytest

import bayarea_lookup as B
import catalog_db as db


def seed(conn, system, rid="one", *, age=1, miss=False, key=True):
    ts = (datetime.now() - timedelta(days=age)).isoformat(timespec="seconds")
    row = dict(record_id=rid, title="Dear zoo", author="Campbell",
               format="picture", isbns=None)
    db.upsert_title(conn, rid, row["title"], ts, row)
    db.upsert_remote_bib(conn, system, rid, ts,
                         None if miss else dict(bib_id="physical", title="Dear zoo", format="BK"),
                         1.0, query_key=B._discovery_key(row, None) if key else None)
    if not miss:
        db.replace_remote_editions(conn, system, rid, [
            dict(bib_id="physical", title="Dear zoo", format_class="picture", kind="primary"),
            dict(bib_id="digital", title="Dear zoo", format_class="ebook", kind="edition"),
        ], ts)
        db.set_edition_details(conn, system, rid, "physical", "", "", '{"isbn": ["123"]}')
        sid = db.record_scrape(conn, "remote", ts)
        db.add_remote_availability(conn, sid, system, rid, "physical", "Dear zoo",
                                   [dict(branch="Main", state="available")], ts)
    conn.commit()
    return row, ts


@pytest.mark.parametrize("system", ["sccl", "sjpl", "linkplus"])
def test_cache_reuses_discovery_but_refreshes_unique_physical_bibs(tmp_path, monkeypatch, system):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, searched_at = seed(conn, system)
    duplicate, _ = seed(conn, system, "two")
    conn.close()
    calls = []

    class Client:
        def search(self, query):
            pytest.fail("a recent match must not be searched again")

        def availability(self, bib):
            calls.append(bib["bib_id"] if isinstance(bib, dict) else bib)
            return [dict(branch="Main", state="out")]

    monkeypatch.setitem(B.SYSTEMS, system, (system, Client))
    B._lookup_system(path, system, [row, duplicate], {}, 0, False, False)
    assert calls == ["physical"]
    conn = db.open_db(path)
    assert {r["state"] for r in db.latest_remote_availability(conn)} == {"out"}
    assert conn.execute("SELECT checked_at FROM remote_bibs WHERE record_id='one'").fetchone()[0] == searched_at
    details = conn.execute("SELECT details FROM remote_editions WHERE record_id='one' AND bib_id='physical'").fetchone()[0]
    assert details == '{"isbn": ["123"]}'
    conn.close()
    # Availability cache is only for this run, never yesterday's response.
    B._lookup_system(path, system, [row, duplicate], {}, 0, False, False)
    assert calls == ["physical", "physical"]


@pytest.mark.parametrize("reason", ["expired", "forced", "title", "author", "format", "isbns", "language", "new", "missing_editions"])
def test_discovery_runs_for_expired_changed_new_or_forced_entries(tmp_path, monkeypatch, reason):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, _ = seed(conn, "sccl", age=8 if reason == "expired" else 1)
    if reason == "new":
        conn.execute("DELETE FROM remote_bibs")
    if reason == "missing_editions":
        conn.execute("DELETE FROM remote_editions")
    conn.commit()
    conn.close()
    if reason in ("title", "author", "format", "isbns"):
        row[reason] = "changed"
    langs = {"one": "chi"} if reason == "language" else {}
    searches = []

    def discover(*args):
        searches.append(args)
        return None, 0.0, []

    monkeypatch.setattr(B, "_discover", discover)
    B._lookup_system(path, "sccl", [row], langs, 0, False, False,
                     rediscover=reason == "forced")
    assert len(searches) == 1


def test_misses_expire_and_retry_misses_overrides_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, ts = seed(conn, "sjpl", miss=True)
    conn.close()
    searches = []
    monkeypatch.setattr(B, "_discover", lambda *args: (searches.append(args) or (None, 0, [])))
    B._lookup_system(path, "sjpl", [row], {}, 0, False, False)
    assert searches == []
    conn = db.open_db(path)
    assert conn.execute("SELECT checked_at FROM remote_bibs").fetchone()[0] == ts
    conn.close()
    B._lookup_system(path, "sjpl", [row], {}, 0, False, True)
    assert len(searches) == 1
    conn = db.open_db(path)
    conn.execute("UPDATE remote_bibs SET checked_at=?",
                 ((datetime.now() - timedelta(days=8)).isoformat(),))
    conn.commit()
    conn.close()
    B._lookup_system(path, "sjpl", [row], {}, 0, False, False)
    assert len(searches) == 2


def test_failed_cached_bib_preserves_snapshot_and_forces_rediscovery(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, ts = seed(conn, "sccl")
    conn.close()

    class Client:
        def availability(self, bib):
            raise RuntimeError("record removed")

    monkeypatch.setitem(B.SYSTEMS, "sccl", ("sccl", Client))
    B._lookup_system(path, "sccl", [row], {}, 0, False, False)
    conn = db.open_db(path)
    assert db.latest_remote_availability(conn)[0]["checked_at"] == ts
    assert conn.execute("SELECT query_key FROM remote_bibs").fetchone()[0] == ""
    conn.close()
    searches = []
    monkeypatch.setattr(B, "_discover", lambda *args: (searches.append(args) or (None, 0, [])))
    B._lookup_system(path, "sccl", [row], {}, 0, False, False)
    assert len(searches) == 1


def test_adopting_legacy_cache_uses_stored_inputs_without_extending_age(tmp_path):
    conn = db.open_db(str(tmp_path / "test.db"))
    row, ts = seed(conn, "sccl", key=False)
    B._seed_discovery_keys(conn, {})
    cached = conn.execute("SELECT * FROM remote_bibs").fetchone()
    assert cached["checked_at"] == ts
    assert cached["query_key"] == B._discovery_key(row, None)
    row["author"] = "different author from edited want-list"
    assert cached["query_key"] != B._discovery_key(row, None)
    conn.close()


@pytest.mark.parametrize("system", ["sccl", "sjpl", "linkplus"])
def test_language_exclusions_prune_cached_editions_before_polling(tmp_path, monkeypatch, system):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, ts = seed(conn, system)
    kept = [dict(e) for e in db.remote_editions(conn)]
    banned = [dict(bib_id=lang, title="Dear zoo", format_class="picture",
                   kind="translation", language=lang) for lang in ("spa", "jpn", "fre")]
    bilingual = dict(bib_id="bilingual", title="Dear zoo", format_class="picture", kind="edition")
    translated = dict(bib_id="translated", title="Dear zoo", format_class="picture", kind="edition")
    db.replace_remote_editions(conn, system, "one", kept + banned + [bilingual, translated], ts)
    db.set_edition_details(conn, system, "one", "bilingual", "", "",
                           '{"notes": "Parallel English and Spanish texts."}')
    db.set_edition_details(conn, system, "one", "translated", "", "",
                           '{"notes": "Translated from the Spanish."}')
    conn.commit()
    conn.close()
    calls = []

    class Client:
        def search(self, query):
            pytest.fail("removing languages should not force rediscovery")

        def availability(self, bib):
            calls.append(bib["bib_id"] if isinstance(bib, dict) else bib)
            return []

    monkeypatch.setitem(B.SYSTEMS, system, (system, Client))
    B._lookup_system(path, system, [row], {}, 0, False, False)
    assert set(calls) == {"physical", "translated"}
    conn = db.open_db(path)
    assert {e["bib_id"] for e in db.remote_editions(conn)} == {"physical", "digital", "translated"}
    assert conn.execute("SELECT checked_at FROM remote_bibs").fetchone()[0] == ts
    conn.close()


def test_excluded_primary_is_unresolved_instead_of_a_cached_miss(tmp_path):
    conn = db.open_db(str(tmp_path / "test.db"))
    seed(conn, "sccl")
    conn.execute("UPDATE remote_editions SET language='spa' WHERE bib_id='physical'")
    assert B.prune_excluded_editions(conn) == 1
    assert conn.execute("SELECT * FROM remote_bibs").fetchall() == []
    assert db.latest_remote_availability(conn) == []
    assert conn.execute("SELECT count(*) FROM remote_availability").fetchone()[0] == 1
    conn.close()


def test_explicit_text_languages_do_not_confuse_original_language():
    for note in ("Text in Spanish and English.", "English and Spanish.",
                 "Parallel text in English and Spanish.", "Text in Japanese.", "Text in French.", "English and French.",
                 "Textos paralelos en inglés y español = Parallel English and Spanish texts."):
        assert B.excluded_edition({"details": {"notes": note}})
    for note in ("Translated from the Spanish.", "Translated from the French.",
                 "Text in simplified Chinese and English translated from Japanese."):
        assert not B.excluded_edition({"details": {"notes": note}})


def test_linkplus_discovery_uses_saved_notes_to_skip_excluded_bibs(tmp_path, monkeypatch):
    conn = db.open_db(str(tmp_path / "test.db"))
    row, ts = seed(conn, "linkplus")
    db.store_raw_page(conn, B._detail_url("linkplus", "spanish"), b"saved detail", ts)
    candidates = [dict(bib_id=bid, title="Dear zoo", authors=["Campbell"],
                       format_class="picture", format="Book", language=None,
                       strict=True) for bid in ("spanish", "english")]
    monkeypatch.setattr(B, "mvpl_details_from_page", lambda page:
                        {"details": {"notes": "English and Spanish."}})

    class Client:
        def search(self, query):
            return candidates

    best, _, editions = B._discover(Client(), "linkplus", row, None, 0, conn)
    assert best["bib_id"] == "english"
    assert [e["bib_id"] for e in editions] == ["english"]
    conn.close()


@pytest.mark.parametrize("lang", ["spa", "jpn", "fre"])
def test_excluded_wants_skip_discovery_polling_and_enrichment(tmp_path, monkeypatch, lang):
    path = str(tmp_path / "test.db")
    conn = db.open_db(path)
    row, _ = seed(conn, "sccl")
    conn.close()
    langs = {"one": lang}
    monkeypatch.setattr(B, "wantlist_langs", lambda: langs)

    def unexpected(*args):
        pytest.fail("an excluded want must not cause a catalog request")

    class Client:
        search = unexpected
        availability = unexpected

    monkeypatch.setitem(B.SYSTEMS, "sccl", ("sccl", Client))
    monkeypatch.setattr(B, "_edition_details", unexpected)
    B._lookup_system(path, "sccl", [row], langs, 0, False, False, rediscover=True)
    assert B.enrich_editions(path, ["sccl"], 0) == 0
