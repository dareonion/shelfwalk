"""Parser and matcher tests against the real pages mirrored in raw_pages.

Hand-written fixtures encode what the markup was believed to be; these run
offline against the bytes each site actually served and assert facts checkable
by hand. They skip when a page is not mirrored, so a fresh clone is not blocked.

    uv run pytest test_mirror.py -q
"""
from __future__ import annotations

import json
import os
import re

import pytest

import acclaim as A
import catalog_db as db
from sources import audies as AU

DB = os.environ.get("SHELFWALK_DB", "shelfwalk.db")


# Children's archive snapshots are checked in, so these parser regressions
# also run on a fresh clone without the personal SQLite database.
def _children_page(url):
    from children import DATA
    manifest = json.loads((DATA / "manifest.json").read_text())
    page = next(p for p in manifest["pages"] if p["url"] == url)
    return (DATA / page["file"]).read_bytes()


@pytest.mark.parametrize("source,first,winners", [
    ("caldecott", 1938, 89), ("newbery", 1922, 105),
    ("geisel", 2006, 21), ("sibert", 2001, 26),
])
def test_children_ala_full_history_winners(source, first, winners):
    from children import DATA
    from sources.children_awards import parse_ala_pdf
    manifest = json.loads((DATA / "manifest.json").read_text())
    url = next(c["url"] for c in manifest["coverage"] if c["source"] == source)
    rows = parse_ala_pdf(_children_page(url), source)
    assert min(r["year"] for r in rows) == first
    assert len([r for r in rows if r["status"] == "winner"]) == winners
    assert all(not r["title"].startswith(("(", ":")) for r in rows)


def test_children_geisel_towed_title_and_current_winner():
    from sources.children_awards import parse_ala_pdf
    url = "https://www.ala.org/sites/default/files/2025-09/geisel-medal-honor-books-to-present.pdf"
    rows = parse_ala_pdf(_children_page(url), "geisel")
    towed = next(r for r in rows if r["title"] == "Towed by Toad")
    assert (towed["year"], towed["status"], towed["author"]) == (2025, "honor", "Jashar Awan")
    assert [(r["title"], r["author"]) for r in rows if r["year"] == 2026 and r["status"] == "winner"] == [("Stop That Mop!", "Jonathan Fenske")]
    baby = next(r for r in rows if r["title"] == "Where’s Baby?")
    assert baby["year"] == 2021  # ALA, not the publisher's erroneous 2020 metadata


def test_children_caldecott_author_and_artist_are_distinct():
    from sources.children_awards import parse_ala_pdf
    url = "https://www.ala.org/sites/default/files/2025-09/caldecott-medal-honors-to-present.pdf"
    rows = parse_ala_pdf(_children_page(url), "caldecott")
    amos = next(r for r in rows if r["title"] == "A Sick Day for Amos McGee")
    assert amos["author"] == "Philip C. Stead"
    assert amos["illustrator"] == "Erin E. Stead"
    assert sum(r["year"] == 2026 for r in rows) == 5


def test_children_carnegie_shortlists_and_longlists_not_conflated():
    from children import CARNEGIE
    from sources.children_awards import parse_carnegie_shortlists
    rows = parse_carnegie_shortlists(_children_page(CARNEGIE + "2026-shortlist-resources/"))
    for category in ("Writing", "Illustration"):
        assert sum(r["category"] == category and r["status"] == "shortlist" for r in rows) == 8
        assert sum(r["category"] == category and r["status"] == "longlist" for r in rows) == 19
    old = parse_carnegie_shortlists(_children_page(CARNEGIE + "2010-2015-shortlist-resources/"))
    assert sum(r["year"] == 2012 and r["category"] == "Illustration" for r in old) == 8


def test_children_zolotow_age_and_combined_cycle():
    from children import ZOLOTOW
    from sources.children_awards import parse_zolotow
    rows = parse_zolotow(_children_page(ZOLOTOW))
    mabel = next(r for r in rows if r["title"] == "Every Monday Mabel")
    assert mabel["status"] == "commended" and mabel["source_min_age"] == 2
    lake = next(r for r in rows if r["title"] == "Our Lake")
    assert lake["status"] == "winner" and lake["source_min_age"] == 5
    evelyn = next(r for r in rows if r["title"] == "Evelyn Del Rey Is Moving Away")
    assert evelyn["year"] == 2022 and evelyn["year_label"] == "2021-2022"


def test_children_csk_retains_all_honors_and_excludes_career_awards():
    from sources.children_awards import parse_csk_year
    rows = parse_csk_year(_children_page("https://www.ala.org/cskbart/2022-winners-and-honors"), 2022)
    assert len(rows) == 10
    assert not any("Nikki Grimes" in r["title"] for r in rows)
    assert sum(r["category"] == "Author" and r["status"] == "honor" for r in rows) == 3


def test_children_hornbook_photographic_titles_and_multiple_honors():
    from children import HORNBOOK
    from sources.children_awards import parse_hornbook
    rows = parse_hornbook(_children_page(HORNBOOK))
    assert min(r["year"] for r in rows) == 1967
    frogs = next(r for r in rows if r["title"] == "Nic Bishop Frogs")
    assert frogs["author"] == "Nic Bishop"
    assert sum(r["year"] == 2025 and r["status"] == "honor" for r in rows) == 6


def test_children_hornbook_reads_italic_as_well_as_emphasis():
    """2014's entries are set in <i> rather than <em>: three winners, six honors."""
    from children import HORNBOOK
    from sources.children_awards import parse_hornbook
    rows = [r for r in parse_hornbook(_children_page(HORNBOOK)) if r["year"] == 2014]
    assert sum(r["status"] == "winner" for r in rows) == 3
    assert sum(r["status"] == "honor" for r in rows) == 6
    assert any(r["title"] == "Mr. Tiger Goes Wild" and r["author"] == "Peter Brown"
               for r in rows)


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


# --- ALA RUSA Listen List -----------------------------------------------------

def _listen_list_panels(conn):
    from sources import listen_list as LL
    return LL.parse_listen_list_panels(_page(conn, LL.LISTEN_LIST_URL))


def _listen_list_archive(conn, year):
    from sources import listen_list as LL
    return LL.parse_listen_list_archive(
        _page(conn, LL.LISTEN_LIST_ARCHIVE[year]), year)


def test_listen_list_panels_carry_2016_onward_twelve_or_so_a_year(conn):
    from collections import Counter
    counts = Counter(e["year"] for e in _listen_list_panels(conn))
    assert counts == {2016: 12, 2017: 12, 2018: 12, 2019: 13, 2020: 13, 2021: 12,
                      2022: 12, 2023: 13, 2024: 12, 2025: 12, 2026: 12}


def test_listen_list_2026_buffalo_hunter_hunter_credit(conn):
    """Author from 'Written by', narrators up to the publisher."""
    e = next(e for e in _listen_list_panels(conn)
             if e["year"] == 2026 and e["title"] == "The Buffalo Hunter Hunter")
    assert e["author"] == "Stephen Graham Jones"
    assert e["narrator"] == "Shane Ghostkeeper, Marin Ireland, and Owen Teale"


def test_listen_list_never_loads_listen_alikes(conn):
    """'The Fire Next Time' and 'The Outsider' are Listen-Alikes under the 2026
    Baldwin selection, not selections."""
    titles = {e["title"] for e in _listen_list_panels(conn) if e["year"] == 2026}
    assert "Baldwin: A Love Story" in titles
    assert not titles & {"The Fire Next Time", "The Outsider"}


def test_listen_list_skips_a_pasted_listen_alike_heading(conn):
    """Two 2023 <h3>s are unquoted Listen-Alike credit lines ('My Life as a
    Goddess…', 'The Shore…') over paragraphs that belong to other titles."""
    titles = [e["title"] for e in _listen_list_panels(conn) if e["year"] == 2023]
    assert not any(t.startswith(("My Life as a Goddess", "The Shore")) for t in titles)
    assert "Playing with Myself" in titles


def test_listen_list_heading_typos_and_heading_narrators(conn):
    """2019 headings carry 'rby', 'by by', a stray 'class' and, once, the
    narrator credit itself."""
    by_title = {e["title"]: e for e in _listen_list_panels(conn) if e["year"] == 2019}
    assert by_title["Dear America: Notes of an Undocumented Citizen"]["author"] == \
        "Jose Antonio Vargas"
    assert by_title["The Trauma Cleaner: One Woman’s Extraordinary Life in the "
                    "Business of Death, Decay, and Disaster"]["author"] == \
        "Sarah Krasnostein"
    assert by_title["I Am, I Am, I Am: Seventeen Brushes with Death"]["author"] == \
        "Maggie O’Farrell"
    silence = by_title["The Silence of the Girls"]
    assert silence["author"] == "Pat Barker"
    assert silence["narrator"] == "Kristin Atherton and Michael Fox"


def test_listen_list_2015_archive_page(conn):
    """The opening quote of 'The Bees' sits outside its <strong>; 'R.C. Bray.'
    ends at the surname, not the initial."""
    entries = _listen_list_archive(conn, 2015)
    assert len(entries) == 12
    by_title = {e["title"]: e for e in entries}
    assert by_title["The Bees"]["author"] == "Laline Paull"
    assert by_title["The Martian"]["author"] == "Andy Weir"
    assert by_title["The Martian"]["narrator"] == "R.C. Bray"


def test_listen_list_press_releases_match_alas_winner_list(conn):
    """2012–2014 selections parsed from the press releases are the same titles
    ALA's own awards page lists for those years (titles compared as work_key
    stems, since the awards page drops subtitles and some articles)."""
    import re
    from html import unescape
    awards = _page(conn, "https://www.ala.org/awards/books-media/listen-list")
    listed: dict = {}
    for title, year in re.findall(
            r'<a href="/winner/[^"]+" hreflang="en">([^<]*)</a>\s*'
            r'<p class="lg:hidden years__field">(\d{4})', awards):
        listed.setdefault(int(year), set()).add(unescape(title).strip())

    def stems(titles):
        return {re.sub(r"^(the|a|an)", "", db.work_key(t).split("|")[0])
                for t in titles}

    for year, n in ((2012, 12), (2013, 13), (2014, 12)):
        parsed = [e["title"] for e in _listen_list_archive(conn, year)]
        assert len(parsed) == n
        assert stems(parsed) == stems(listed[year]), year


# --- Audible: best of the year and bestseller charts --------------------------

def test_audible_hub_2025_audiobook_of_the_year_and_top_20(conn):
    """The 2025 Audiobook of the Year is Atmosphere (Taylor Jenkins Reid, read by
    Julia Whelan and Kristen DiMercurio), followed by the Top 20."""
    from sources import audible as AU
    hub = AU.parse_boty_hub(_page(conn, AU.AUDIBLE_BOTY_HUB))
    assert hub["year"] == 2025
    aoty = [e for e in hub["entries"] if e["category"] == "Audiobook of the Year"]
    assert aoty == [{"category": "Audiobook of the Year", "title": "Atmosphere",
                     "author": "Taylor Jenkins Reid",
                     "narrator": "Julia Whelan, Kristen DiMercurio"}]
    top = [e for e in hub["entries"] if e["category"] == "Top 20"]
    assert len(top) == 20


def test_audible_hub_narrators_stay_inside_their_own_item(conn):
    """Narrator links are read per item: Mel Robbins reads The Let Them Theory
    herself (no link), so Jefferson White belongs only to Sunrise on the Reaping."""
    from sources import audible as AU
    top = {e["title"]: e for e in AU.parse_boty_hub(_page(conn, AU.AUDIBLE_BOTY_HUB))["entries"]}
    assert top["The Buffalo Hunter Hunter"]["narrator"] == \
        "Shane Ghostkeeper, Marin Ireland, Owen Teale"
    assert top["The Let Them Theory"]["narrator"] is None
    assert top["Sunrise on the Reaping"]["narrator"] == "Jefferson White"
    # headings keep the punctuation the aria-labels drop
    assert "Someday, Now" in top and "If Anyone Builds It, Everyone Dies" in top
    assert top["If Anyone Builds It, Everyone Dies"]["author"] == "Eliezer Yudkowsky"


def test_audible_tag_page_lists_every_year_and_skips_app_tests(conn):
    from sources import audible as AU
    first = AU.parse_boty_tag(_page(conn, AU.AUDIBLE_BOTY_TAG))
    assert first["last_page"] == 6
    assert ("/blog/article-2025-best-fiction-audiobooks", 2025) in first["articles"]
    fifth = AU.parse_boty_tag(_page(conn, AU.AUDIBLE_BOTY_TAG + "/page/5"))
    paths = [p for p, _ in fifth["articles"]]
    assert "/blog/article-2022-best-fantasy-audiobooks" in paths
    assert not any(p.endswith("-app-test") for p in paths)


def test_audible_blog_list_reads_the_json_ld_picks(conn):
    """Each pick is a schema.org Audiobook block with author and readBy."""
    from sources import audible as AU
    path = "/blog/article-2025-best-sci-fi-fantasy-audiobooks"
    lst = AU.parse_blog_list(_page(conn, AU.AUDIBLE + path), path)
    assert (lst["year"], lst["list"]) == (2025, "The 10 best sci-fi & fantasy listens of 2025")
    assert len(lst["entries"]) == 10
    first = lst["entries"][0]
    assert (first["title"], first["author"], first["narrator"]) == \
        ("The Incandescent", "Emily Tesh", "Zara Ramm")
    assert any(e["title"] == "This Inevitable Ruin" and e["author"] == "Matt Dinniman"
               for e in lst["entries"])


def test_audible_blog_list_2022_uses_the_same_structure(conn):
    from sources import audible as AU
    path = "/blog/article-2022-best-fiction-audiobooks"
    lst = AU.parse_blog_list(_page(conn, AU.AUDIBLE + path), path)
    assert lst["year"] == 2022 and len(lst["entries"]) == 15
    assert lst["list"] == "The 15 Best Fiction Audiobooks of 2022"
    assert lst["entries"][0]["title"] == "Our Missing Hearts"
    assert lst["entries"][0]["narrator"] == "Lucy Liu"


def test_audible_chart_top_20_with_ranks_authors_and_narrators(conn):
    from sources import audible as AU
    chart = AU.parse_chart(_page(conn, AU.AUDIBLE_CHART))
    assert chart["chart"] == "Bestselling Audiobooks"
    assert [e["rank"] for e in chart["entries"]] == list(range(1, 21))
    assert all(e["title"] and e["author"] for e in chart["entries"])
    dcc = next(e for e in chart["entries"] if e["title"] == "Dungeon Crawler Carl")
    assert (dcc["author"], dcc["narrator"], dcc["series"]) == \
        ("Matt Dinniman", "Jeff Hays", "Dungeon Crawler Carl, Book 1")


def test_audible_chart_links_only_query_free_category_charts(conn):
    """robots.txt disallows /charts/*?; category chart paths carry no query."""
    from sources import audible as AU
    chart = AU.parse_chart(_page(conn, AU.AUDIBLE_CHART))
    assert "/charts/best/science-fiction-fantasy-audiobooks/18580606011" in chart["categories"]
    assert all("?" not in p for p in chart["categories"])
    sff = AU.parse_chart(_page(
        conn, AU.AUDIBLE + "/charts/best/science-fiction-fantasy-audiobooks/18580606011"))
    assert sff["chart"] == "Bestselling Science Fiction & Fantasy Audiobooks"
    assert len(sff["entries"]) == 20


# --- audiobook popularity charts ----------------------------------------------
#
# Snapshots change on every fetch, so these check shape, filters and title
# cleaning rather than any title's rank.

def _mirrored(conn, url):
    raw = db.get_raw_page(conn, url)
    if raw is None:
        pytest.skip(f"not mirrored: {url}")
    return raw


def test_librofm_chart_is_a_complete_top_100(conn):
    from sources.librofm import LIBROFM_URL, parse_librofm
    entries = parse_librofm(_mirrored(conn, LIBROFM_URL).decode("utf-8", "replace"))
    assert [e["rank"] for e in entries] == list(range(1, 101))
    assert entries[0]["title"] and entries[0]["authors"]
    assert all(e["title"] and e["authors"] for e in entries)
    assert sum(1 for e in entries if e["narrators"]) >= 95


def test_librofm_titles_lose_book_club_and_series_tags(conn):
    from sources.librofm import LIBROFM_URL, clean_librofm_title, parse_librofm
    assert clean_librofm_title("Kin: Oprah&#39;s Book Club") == "Kin"
    assert clean_librofm_title("Atmosphere: A GMA Book Club Pick") == "Atmosphere"
    assert clean_librofm_title("The Witches of Cambridge (A Read with Jenna Pick)") == \
        "The Witches of Cambridge"
    assert clean_librofm_title("Sunrise on the Reaping (The Hunger Games)") == \
        "Sunrise on the Reaping"
    titles = [e["title"] for e in
              parse_librofm(_mirrored(conn, LIBROFM_URL).decode("utf-8", "replace"))]
    assert not any("Book Club" in t or "(" in t or "&#" in t for t in titles)


def test_apple_audio_chart_drops_kids_and_storefront_decoration(conn):
    from sources.apple_audio import (APPLE_AUDIO_URL, APPLE_KIDS_GENRE,
                                     parse_apple_audio)
    raw = _mirrored(conn, APPLE_AUDIO_URL)
    results = json.loads(raw)["feed"]["results"]
    kids = sum(1 for r in results
               if any(g.get("name") == APPLE_KIDS_GENRE for g in r.get("genres", [])))
    snap, entries = parse_apple_audio(raw)
    assert len(results) == 100
    assert snap and len(snap) == 10
    assert len(entries) == 100 - kids
    assert entries[0]["title"] and entries[0]["author"]
    assert not any("(Unabridged)" in e["title"] or "[" in e["title"]
                   or ", Book " in e["title"] for e in entries)


def test_apple_audio_title_and_author_cleaning():
    from sources.apple_audio import clean_apple_title, first_author
    assert clean_apple_title(
        "Carl's Doomsday Scenario: Dungeon Crawler Carl, Book 2 (Unabridged)") == \
        "Carl's Doomsday Scenario"
    assert clean_apple_title("Red Rising (Red Rising)") == "Red Rising"
    assert clean_apple_title("Yesteryear: A GMA Book Club Pick: A Novel (Unabridged)") == \
        "Yesteryear: A Novel"
    assert first_author("James Patterson & James O. Born") == "James Patterson"
    assert first_author("Bessel van der Kolk, M.D.") == "Bessel van der Kolk"


LIBBY_PAGE1 = ("https://thunder.api.overdrive.com/v2/libraries/santaclara/media"
               "?mediaTypes=audiobook&perPage=100&page=1&sortBy=popularity")


def test_libby_page_carries_authors_and_narrators(conn):
    from sources.libby_audio import parse_libby_page
    rows = parse_libby_page(_mirrored(conn, LIBBY_PAGE1))
    assert len(rows) == 100
    assert [r["rank"] for r in rows] == list(range(1, 101))
    adult = [r for r in rows if r["adult_english"]]
    assert adult[0]["title"] and adult[0]["author"]
    assert sum(1 for r in adult if r["narrators"]) >= len(adult) - 2


def test_libby_skips_juvenile_young_adult_and_non_english(conn):
    from sources.libby_audio import libby_is_adult_english, parse_libby_page
    raw = _mirrored(conn, LIBBY_PAGE1)
    items = json.loads(raw)["items"]
    youth = [i for i in items if (i.get("ratings", {}).get("maturityLevel") or {})
             .get("id") in ("juvenile", "youngadult")]
    assert youth, "page 1 of the OverDrive-wide chart always carries youth titles"
    assert not any(libby_is_adult_english(i) for i in youth)
    rows = {r["title"]: r for r in parse_libby_page(raw)}
    assert all(not rows[i["title"]]["adult_english"] for i in youth)
    assert libby_is_adult_english({"ratings": {"maturityLevel": {"id": "generalcontent"}},
                                   "languages": [{"id": "es"}]}) is False


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
    for r in rows:
        src = A.SOURCES.get(r["source"])
        if src is not None and src.kind != "award":   # lists and charts have none
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
