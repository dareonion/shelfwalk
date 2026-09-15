"""Parser and matcher tests against the real pages mirrored in raw_pages.

Hand-written fixtures encode what the markup was believed to be; these run
offline against the bytes each site actually served and assert facts checkable
by hand. They skip when a page is not mirrored, so a fresh clone is not blocked.

    uv run pytest test_mirror.py -q
"""
from __future__ import annotations

import os
import re

import pytest

import acclaim as A
import catalog_db as db
from sources import audies as AU

DB = os.environ.get("SHELFWALK_DB", "shelfwalk.db")


@pytest.fixture(scope="module")
def conn():
    if not os.path.exists(DB):
        pytest.skip(f"{DB} not present")
    return db.open_db(DB)


def _page(conn, url, decoder=None):
    raw = db.get_raw_page(conn, url)
    if raw is None:
        pytest.skip(f"not mirrored: {url}")
    return (decoder or (lambda b: b.decode("utf-8", "replace")))(raw)


# --- sfadb ---------------------------------------------------------------------

def test_sfadb_2024_short_story_winner_is_kritzer(conn):
    """The 2024 Hugo for Best Short Story went to Naomi Kritzer's 'Better Living
    Through Algorithms' (Clarkesworld)."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    shorts = [e for e in A.parse_sfadb_year(page) if e["form"] == "short-story"]
    winners = [e for e in shorts if e["status"] == "winner"]
    assert len(winners) == 1
    assert winners[0]["title"] == "Better Living Through Algorithms"
    assert winners[0]["author"] == "Naomi Kritzer"
    assert winners[0]["publisher"].startswith("Clarkesworld")


def test_sfadb_2024_short_story_has_a_full_ballot(conn):
    """A Hugo category carries a winner plus five finalists; fewer than six means
    the parser is dropping entries."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    shorts = [e for e in A.parse_sfadb_year(page) if e["form"] == "short-story"]
    assert len(shorts) >= 6


def test_sfadb_never_stores_an_anthology_as_the_story(conn):
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    titles = {e["title"] for e in A.parse_sfadb_year(page)
              if e["form"] == "short-story"}
    assert not any(t.startswith("Galaxy's Edge Vol.") for t in titles)
    assert not any(t.lower() == "winner" for t in titles)


def test_sfadb_pages_are_latin1_and_accents_survive(conn):
    """sfadb serves Latin-1; 'P. Djèlí Clark' must come through intact."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    authors = {e["author"] for e in A.parse_sfadb_year(page) if e["author"]}
    assert any("Djèlí" in a for a in authors)
    assert not any("�" in a for a in authors)


def test_sfadb_novel_category_still_uses_bold_titles(conn):
    """Novel titles are bolded, not quoted, and must still parse."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    novels = [e for e in A.parse_sfadb_year(page) if e["form"] == "novel"]
    assert len(novels) >= 6
    assert all(e["title"] and len(e["title"]) < 120 for e in novels)


# --- Pulitzer: the browser harvest ---------------------------------------------

def test_pulitzer_2026_fiction_is_angel_down():
    """Checked by hand against pulitzer.org."""
    path = os.path.join(A.HARVEST_DIR, "pulitzer.json")
    if not os.path.exists(path):
        pytest.skip("pulitzer harvest not present")
    import json
    rows = json.load(open(path, encoding="utf-8"))
    fic = [r for r in rows if r["category"] == "Fiction" and r["year"] == 2026]
    win = [A.parse_credit(r["raw"]) for r in fic if r["status"] == "winner"]
    fin = [A.parse_credit(r["raw"])["author"] for r in fic
           if r["status"] == "finalist"]
    assert len(win) == 1 and win[0]["title"] == "Angel Down"
    assert win[0]["author"] == "Daniel Kraus"
    assert "Katie Kitamura" in fin and "Torrey Peters" in fin


def test_pulitzer_finalists_start_in_1980():
    """The Pulitzer only began publishing finalists in 1980; earlier finalist
    rows would mean the year column is being misread."""
    path = os.path.join(A.HARVEST_DIR, "pulitzer.json")
    if not os.path.exists(path):
        pytest.skip("pulitzer harvest not present")
    import json
    rows = json.load(open(path, encoding="utf-8"))
    years = [r["year"] for r in rows if r["status"] == "finalist" and r["year"]]
    assert min(years) >= 1980


# --- BiblioCommons: the acclaim shelf join -------------------------------------

@pytest.fixture
def mirrored_catalog(conn, monkeypatch):
    """Serve every catalog request from raw_pages; skip when one isn't there."""
    import bayarea_lookup as B

    def get(url, **_kw):
        raw = db.get_raw_page(conn, url)
        if raw is None:
            pytest.skip(f"not mirrored: {url}")
        return raw
    monkeypatch.setattr(B, "_get", get)


def test_every_mirrored_bibliocommons_format_is_classified(conn):
    """Unknown codes fall to 'other', which is safe but silent — so a code the
    catalogs start sending must be placed deliberately, here."""
    import json
    import zlib

    import bayarea_lookup as B
    known = (B.BC_BOOK_FORMATS | B._BC_AUDIO_FORMATS | B._BC_PRINT_FORMATS
             | B._BC_NONBOOK_FORMATS
             | {"BOARD_BK", "PICTURE_BOOK", "EBOOK", "EAUDIOBOOK",
                "GRAPHIC_NOVEL_DOWNLOAD"})
    seen = set()
    for (body,) in conn.execute(
            "SELECT body_gz FROM raw_pages WHERE host = 'gateway.bibliocommons.com'"
            " AND url LIKE '%/bibs/search%'"):
        bibs = json.loads(zlib.decompress(body)).get("entities", {}).get("bibs") or {}
        seen |= {(b.get("briefInfo") or {}).get("format") for b in bibs.values()}
    seen.discard(None)
    if not seen:
        pytest.skip("no BiblioCommons searches mirrored")
    assert seen - known == set()


def test_shelf_home_is_marilynne_robinsons_not_every_robinson_home(mirrored_catalog):
    """SCCL's search also returns Peter Robinson's 'Close to Home', 'Stealing
    Home' and DVDs such as 'Home Page'; only her print Home is kept."""
    kept = A.shelf_candidates("sccl", "Home", "Marilynne Robinson")
    assert kept
    assert all(c["authors"] == ["Robinson, Marilynne"] for c in kept)
    assert all(c["title"] == "Home" for c in kept)


def test_shelf_never_counts_a_dvd_as_the_novel(mirrored_catalog):
    kept = A.shelf_candidates("paloalto", "Euphoria", "Lily King")
    assert kept and all(c["format"] in ("BK", "LPRINT") for c in kept)
    railroad = A.shelf_candidates("sccl", "The Underground Railroad",
                                  "Colson Whitehead")
    assert railroad and not {c["format"] for c in railroad} & {"DVD", "BLURAY"}


def test_shelf_skips_a_translation_that_shares_the_title(mirrored_catalog):
    """SJPL's Chinese edition is 'Miao xiao yi sheng: A little life'."""
    kept = A.shelf_candidates("sjpl", "A Little Life", "Hanya Yanagihara")
    assert kept and all(c["language"] == "eng" for c in kept)


def test_shelf_finds_a_volume_filed_under_its_series(mirrored_catalog):
    kept = A.shelf_candidates("sccl", "Master of the Senate", "Robert A. Caro")
    assert any(c["title"].startswith("The Years of Lyndon Johnson") for c in kept)


def test_shelf_matches_an_accented_credit(mirrored_catalog):
    """SCCL credits the print *Trust* to 'Díaz, Hernán'; the corpus says Diaz."""
    kept = A.shelf_candidates("sccl", "Trust", "Hernan Diaz")
    assert any("Díaz" in a for c in kept for a in c["authors"])


def test_shelf_searches_the_first_of_several_authors(mirrored_catalog):
    """SCCL's print Abundance credits only Klein, so the search uses the first
    credited author."""
    kept = A.shelf_candidates("sccl", "Abundance", "Ezra Klein & Derek Thompson")
    assert any(c["format"] == "BK" and c["authors"] == ["Klein, Ezra"] for c in kept)


# --- Audie Awards: every mirrored year page -----------------------------------

def _audie_year(conn, year):
    page = _page(conn, AU.AUDIE_BASE + AU.AUDIE_YEARS[year])
    return page, AU.parse_audie_any(page)


def _one(entries, category, status="winner"):
    got = [e for e in entries if e["category"] == category and e["status"] == status]
    assert len(got) == 1, (category, status, got)
    return got[0]


def test_audie_audiobook_of_the_year_winners(conn):
    """Read off each year's page, including the 2008 header that drops 'OF'."""
    expected = {
        2008: ("The Chopin Manuscript: A Serial Thriller", "Alfred Molina"),
        2009: ("The Graveyard Book", "Neil Gaiman"),
        2017: ("Hamilton: The Revolution", "Mariska Hargitay"),
        2022: ("Project Hail Mary", "Ray Porter"),
    }
    for year, (title, narrator) in expected.items():
        _, entries = _audie_year(conn, year)
        e = _one(entries, "Audiobook Of The Year")
        assert (e["title"], e["narrator"]) == (title, narrator), year
    _, entries = _audie_year(conn, 2022)
    assert _one(entries, "Audiobook Of The Year")["author"] == "Andy Weir"
    _, entries = _audie_year(conn, 2017)
    assert _one(entries, "Audiobook Of The Year")["author"] == \
        "Lin-Manuel Miranda and Jeremy McCarter"


def test_audie_narrator_categories_name_the_book_not_the_narrator(conn):
    """2022 onward list '<Narrator> / for / <title> / By <Author>'."""
    _, entries = _audie_year(conn, 2023)
    male = _one(entries, "Best Male Narrator")
    assert (male["title"], male["author"], male["narrator"]) == \
        ("Fairy Tale", "Stephen King", "Seth Numrich")
    female = _one(entries, "Best Female Narrator")
    assert (female["title"], female["author"], female["narrator"]) == \
        ("The Eye of the World", "Robert Jordan", "Rosamund Pike")
    for year in (2022, 2023, 2024, 2025, 2026):
        _, entries = _audie_year(conn, year)
        narr = [e for e in entries if "Narrator" in e["category"]]
        assert narr and all(e["title"] != e["narrator"] for e in narr), year


def test_audie_modern_entries_carry_their_author(conn):
    """'By <Author>' and 'Narrated by <Narrator>' sit on separate lines."""
    _, entries = _audie_year(conn, 2024)
    tom = [e for e in entries if e["title"] == "Tom Lake"]
    assert tom and all(e["author"] == "Ann Patchett" for e in tom)
    assert all(e["narrator"] == "Meryl Streep" for e in tom)
    for year in (2022, 2023, 2024, 2025, 2026):
        _, entries = _audie_year(conn, year)
        assert sum(1 for e in entries if not e["author"]) == 0, year


def test_audie_legacy_pages_include_finalists(conn):
    """Before 2017 a finalist is '<title>' then 'by <A>; narrated by <N> (<P>)'."""
    _, entries = _audie_year(conn, 1996)
    win = _one(entries, "Children’s Title")
    assert (win["title"], win["author"], win["narrator"]) == \
        ("Jumanji", "Chris Van Allsburg", "Robin Williams")
    fin = {e["title"]: e for e in entries
           if e["category"] == "Children’s Title" and e["status"] == "finalist"}
    assert set(fin) == {"Octopus Lady and Crow", "Toy Story Read-Along"}
    assert fin["Octopus Lady and Crow"]["author"] == "Johnny Moses"
    assert fin["Octopus Lady and Crow"]["narrator"] == "Johnny Moses"


def test_audie_every_category_on_every_page_yields_entries(conn):
    """A category header with nothing parsed under it means a layout slipped.
    The 2018 marketing winner is a campaign the page gives no credit for."""
    no_credit = {(2018, "Excellence In Marketing", "winner")}
    for year in sorted(AU.AUDIE_YEARS):
        page, entries = _audie_year(conn, year)
        lines = AU._audie_lines(page)
        lines = lines[:lines.index("APA LINKS")]
        headers = {AU._header(x) for x in lines if AU._header(x)}
        headers = {h for h in headers
                   if not AU._AU_PERSON_CATEGORY_RE.search(h[0])}
        parsed = {(e["category"], e["status"]) for e in entries}
        missing = {h for h in headers - parsed if (year, *h) not in no_credit}
        assert not missing, (year, missing)


def test_audie_modern_pages_parse_every_published_entry(conn):
    """From 2022 each entry ends in a 'Published by' line."""
    for year in (2022, 2023, 2024, 2025, 2026):
        page, entries = _audie_year(conn, year)
        published = sum(1 for x in AU._audie_lines(page) if x.startswith("Published by"))
        assert len(entries) >= published - 2, (year, len(entries), published)


def test_audie_titles_are_never_credits(conn):
    """A credit line never becomes a title ('Written in My Own Heart's Blood'
    is a real one)."""
    for year in sorted(AU.AUDIE_YEARS):
        _, entries = _audie_year(conn, year)
        bad = [e["title"] for e in entries
               if re.match(r"^(?:(?:written|narrated|performed|published)"
                           r"(?: and \w+)? )?by\b", e["title"], re.I)]
        assert not bad, (year, bad)


# --- every loader, offline ------------------------------------------------------

def test_every_registered_loader_runs_offline(conn, monkeypatch, tmp_path):
    """Each SOURCES loader completes against the mirror and harvest/ without
    raising. Unmirrored pages fail like a dead link, so this checks the code
    paths (names, imports, signatures), not coverage. A browser-tier source
    with no harvest on this machine is skipped."""
    import urllib.request

    import bayarea_lookup as B

    def get(url, **_kw):
        raw = db.get_raw_page(conn, url)
        if raw is None:
            raise RuntimeError(f"not mirrored: {url}")
        return raw

    def no_network(*_a, **_kw):
        raise RuntimeError("network access in a test")

    monkeypatch.setattr(B, "_get", get)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    scratch = db.open_db(str(tmp_path / "loaders.db"))
    monkeypatch.setattr(B, "_archive_path", None)
    errors = {}
    for key, source in A.SOURCES.items():
        harvested = any(os.path.exists(os.path.join(A.HARVEST_DIR, name))
                        for name in (f"{key}.json", key))
        if source.transport == A.BROWSER and not harvested:
            continue
        try:
            source.load(scratch)
        except Exception as exc:                          # noqa: BLE001
            errors[key] = f"{type(exc).__name__}: {exc}"
    assert errors == {}


# --- the loaded corpus: shape assertions ----------------------------------------

def test_every_source_has_at_least_one_winner(conn):
    """A source with nominees but no winners has almost certainly lost its winner
    marker."""
    rows = conn.execute(
        "SELECT source, SUM(status IN ('winner')) w, COUNT(*) n "
        "FROM accolades GROUP BY source").fetchall()
    if not rows:
        pytest.skip("corpus not loaded")
    listy = {"nyt", "wsj", "obama", "douban"}      # lists have no winners
    for r in rows:
        if r["source"] in listy:
            continue
        assert r["w"] > 0, f"{r['source']} has {r['n']} rows and no winners"


def test_no_work_key_collapses_to_an_empty_title(conn):
    """A key stripped to [a-z0-9] folds every CJK title to ''."""
    rows = conn.execute(
        "SELECT work_key, title FROM works WHERE work_key LIKE '|%'").fetchall()
    if rows is None:
        pytest.skip("corpus not loaded")
    # a genuinely punctuation-only title (Fady Joudah's '[...]') is legitimate
    bad = [r for r in rows if any(c.isalnum() for c in (r["title"] or ""))]
    assert not bad, f"{len(bad)} works lost their title in the key: {bad[:3]}"


def test_short_fiction_is_actually_present(conn):
    """Hugo, Nebula and Locus short fiction runs to thousands of rows; far fewer
    means titles are being dropped."""
    n = conn.execute(
        "SELECT COUNT(*) FROM accolades a JOIN works w USING(work_key) "
        "WHERE a.source IN ('hugo','nebula','locus') "
        "AND w.form IN ('novella','novelette','short-story')").fetchone()[0]
    if n == 0:
        pytest.skip("SF awards not loaded")
    assert n > 4000, f"only {n} short-fiction rows; sfadb short fiction is under-parsed"


def test_audiobook_awards_carry_narrators(conn):
    n_audie = conn.execute(
        "SELECT COUNT(*) FROM accolades WHERE source = 'audies'").fetchone()[0]
    if not n_audie:
        pytest.skip("audies not loaded")
    with_nar = conn.execute(
        "SELECT COUNT(*) FROM accolades WHERE source = 'audies' "
        "AND narrator IS NOT NULL").fetchone()[0]
    assert with_nar > n_audie * 0.8
