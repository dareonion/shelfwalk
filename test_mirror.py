"""Parser tests against the REAL mirrored pages, not hand-written fixtures.

Every fixture in `test_acclaim.py` encodes what I *believed* the markup was.
That belief was wrong five separate times, and each time the parser returned
less while reporting success:

    sfadb   short-fiction titles are quoted, not bolded  (⅔ of it lost)
    FT      {{blue ribbon}} vs {{Blue ribbon}}           (14 of 21 winners)
    Audies  no "Published by" line before ~2017          (21 years lost)
    ISFDB   author sits past a 4000-char window          (0 containers)
    NYT     titles wrapping onto two lines               (6 of 100 lost)

`raw_pages` holds ~5,900 mirrored responses, so these run offline against the
bytes the site actually served, and assert against facts checkable by hand.
They skip rather than fail when a page is not mirrored, so a fresh clone is
not blocked — but on Darren's machine they are the ones that would catch a
site quietly changing its layout on the next scrape.

    uv run pytest test_mirror.py -q
"""
from __future__ import annotations

import os

import pytest

import acclaim as A
import catalog_db as db

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


# --- sfadb: the bug that hid two-thirds of the short fiction --------------------

def test_sfadb_2024_short_story_winner_is_kritzer(conn):
    """Known truth: the 2024 Hugo for Best Short Story went to Naomi Kritzer's
    'Better Living Through Algorithms' (Clarkesworld). Before the quoted-title
    fix this parsed as 'Galaxy's Edge Vol. 13' — an anthology name."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    shorts = [e for e in A.parse_sfadb_year(page) if e["form"] == "short-story"]
    winners = [e for e in shorts if e["status"] == "winner"]
    assert len(winners) == 1
    assert winners[0]["title"] == "Better Living Through Algorithms"
    assert winners[0]["author"] == "Naomi Kritzer"
    assert winners[0]["publisher"].startswith("Clarkesworld")


def test_sfadb_2024_short_story_has_a_full_ballot(conn):
    """A Hugo category carries a winner plus five finalists. Anything much
    under six means the parser is dropping entries again."""
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
    """Decoding sfadb as UTF-8 turned 'P. Djèlí Clark' into replacement
    characters, and that string became the author in the corpus."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    authors = {e["author"] for e in A.parse_sfadb_year(page) if e["author"]}
    assert any("Djèlí" in a for a in authors)
    assert not any("�" in a for a in authors)


def test_sfadb_novel_category_still_uses_bold_titles(conn):
    """The quoted-title fix must not break the bolded-novel path."""
    page = _page(conn, "https://www.sfadb.com/Hugo_Awards_2024", A._decode_page)
    novels = [e for e in A.parse_sfadb_year(page) if e["form"] == "novel"]
    assert len(novels) >= 6
    assert all(e["title"] and len(e["title"]) < 120 for e in novels)


# --- Pulitzer: the browser harvest ---------------------------------------------

def test_pulitzer_2026_fiction_is_angel_down():
    """Confirmed live against pulitzer.org during the build."""
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


# --- the loaded corpus: shape assertions ----------------------------------------

def test_every_source_has_at_least_one_winner(conn):
    """A source with nominees but no winners has almost certainly lost its
    winner marker — the FT lost 14 of 21 that way."""
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
    """Stripping to [a-z0-9] folded every CJK title to '' and collapsed 540
    Douban books into 26 keys."""
    rows = conn.execute(
        "SELECT work_key, title FROM works WHERE work_key LIKE '|%'").fetchall()
    if rows is None:
        pytest.skip("corpus not loaded")
    # a genuinely punctuation-only title (Fady Joudah's '[...]') is legitimate
    bad = [r for r in rows if any(c.isalnum() for c in (r["title"] or ""))]
    assert not bad, f"{len(bad)} works lost their title in the key: {bad[:3]}"


def test_short_fiction_is_actually_present(conn):
    """The sfadb bug left ~1,800 short-fiction rows where there should be
    ~5,400. This is the regression guard for that."""
    n = conn.execute(
        "SELECT COUNT(*) FROM accolades a JOIN works w USING(work_key) "
        "WHERE a.source IN ('hugo','nebula','locus') "
        "AND w.form IN ('novella','novelette','short-story')").fetchone()[0]
    if n == 0:
        pytest.skip("SF awards not loaded")
    assert n > 4000, f"only {n} short-fiction rows; the quoted-title bug is back"


def test_audiobook_awards_carry_narrators(conn):
    n_audie = conn.execute(
        "SELECT COUNT(*) FROM accolades WHERE source = 'audies'").fetchone()[0]
    if not n_audie:
        pytest.skip("audies not loaded")
    with_nar = conn.execute(
        "SELECT COUNT(*) FROM accolades WHERE source = 'audies' "
        "AND narrator IS NOT NULL").fetchone()[0]
    assert with_nar > n_audie * 0.8
