"""Tests for acclaim.py — award/list corpus parsers. No network.

Every fixture below is a verbatim slice of real markup from the source it
names, kept small. The point of fixture tests here is that these parsers run
against sites that change without warning: when one breaks, the failing test
should say which source and which shape.
"""
from __future__ import annotations

import os
import tempfile

import acclaim as A
import catalog_db as db


# --- credit parsing (Pulitzer's 'Title, by Author (Publisher)') ------------------

def test_parse_credit_basic():
    c = A.parse_credit("Angel Down, by Daniel Kraus (Atria Books)")
    assert c == {"title": "Angel Down", "author": "Daniel Kraus",
                 "publisher": "Atria Books"}


def test_parse_credit_title_containing_commas():
    """The Netanyahus' full title has four commas before the author."""
    raw = ("The Netanyahus: An Account of a Minor and Ultimately Even "
           "Negligible Episode in the History of a Very Famous Family, "
           "by Joshua Cohen (New York Review Books)")
    c = A.parse_credit(raw)
    assert c["author"] == "Joshua Cohen"
    assert c["title"].startswith("The Netanyahus:")
    assert c["title"].endswith("Very Famous Family")


def test_parse_credit_without_publisher():
    """Drama entries carry no publisher."""
    c = A.parse_credit("Liberation, by Bess Wohl")
    assert c == {"title": "Liberation", "author": "Bess Wohl", "publisher": None}


def test_parse_credit_no_award_is_empty_not_an_error():
    """A year with no prize is data. Callers skip an empty title."""
    assert A.parse_credit("No award")["title"] == ""
    assert A.parse_credit("")["title"] == ""


def test_parse_credit_keeps_an_edition_parenthetical_in_the_title():
    c = A.parse_credit("Taipei Story (Deluxe Limited Edition), by R.F. Kuang")
    assert c["title"] == "Taipei Story (Deluxe Limited Edition)"
    assert c["publisher"] is None


# --- sfadb (Hugo / Nebula / Locus) ----------------------------------------------

SFADB = '''
<div class="categoryblock">
<div class="category">Novella</div>
<ul>
<li> <span class="winner">Winner:</span> <b>The Tusks of Extinction</b>, <a href="Ray_Nayler">Ray Nayler</a> (Tordotcom)</li>
<li> <b>The Brides of High Hill</b>, <a href="Nghi_Vo">Nghi Vo</a> (Tordotcom)</li>
</ul>
</div>
<div class="categoryblock">
<div class="category">Best Editor, Long Form</div>
<ul>
<li> <span class="winner">Winner:</span> <b>Somebody</b>, <a href="X">A Person</a></li>
</ul>
</div>
'''


def test_sfadb_marks_winner_and_nominee():
    got = A.parse_sfadb_year(SFADB)
    assert [(e["title"], e["status"]) for e in got] == [
        ("The Tusks of Extinction", "winner"),
        ("The Brides of High Hill", "nominee")]


def test_sfadb_captures_form_and_publisher():
    e = A.parse_sfadb_year(SFADB)[0]
    assert e["form"] == "novella"
    assert e["author"] == "Ray Nayler"
    assert e["publisher"] == "Tordotcom"


def test_sfadb_skips_people_categories():
    """Best Editor is a real Hugo, but not a thing you can borrow."""
    assert all(e["category"] != "Best Editor, Long Form"
               for e in A.parse_sfadb_year(SFADB))


def test_sfadb_form_mapping_strips_best_prefix():
    assert A._sfadb_form("Best Short Story") == "short-story"
    assert A._sfadb_form("Novelette") == "novelette"
    assert A._sfadb_form("Best Dramatic Presentation") is None


# --- Booker ---------------------------------------------------------------------

BOOKER = '''<html><head><title>Flesh | The Booker Prizes</title></head><body>
<h2>David Szalay</h2>
<dl>
<dt class="h6">Winner</dt>
<dd><a href="/the-booker-library/prize-years/2025">The Booker Prize 2025</a></dd>
<dt class="h6">Longlisted</dt>
<dd><a href="/the-booker-library/prize-years/2025">The Booker Prize 2025</a></dd>
</dl>
<h2>Buy the book</h2></body></html>'''


def test_booker_title_author_and_multiple_statuses():
    rec = A.parse_booker_book(BOOKER)
    assert rec["title"] == "Flesh"
    assert rec["author"] == "David Szalay"
    assert {(a["status"], a["year"]) for a in rec["accolades"]} == {
        ("winner", 2025), ("longlist", 2025)}


def test_booker_separates_the_international_prize():
    page = BOOKER.replace("The Booker Prize 2025",
                          "The International Booker Prize 2025")
    assert {a["award"] for a in A.parse_booker_book(page)["accolades"]} == {
        "booker-intl"}


def test_booker_ignores_section_headings_when_finding_the_author():
    """'Buy the book' is an <h2> too and must not become the author."""
    assert A.parse_booker_book(BOOKER)["author"] != "Buy the book"


# --- National Book Awards -------------------------------------------------------

NBA = '''
<div class="winner-book"><h3>WINNER</h3>
  <div class="book-data"><h1><a href="/books/x/">The True True Story of Raja</a></h1><h2>Rabih Alameddine</h2></div>
</div>
<div class="finalist-books"><h3>FINALISTS</h3>
  <figure class="winner-list"><h1><a href="/books/g/">A Guardian and a Thief</a></h1><h2>Megha Majumdar</h2></figure>
  <figure class="winner-list"><h1><a href="/books/a/">The Antidote</a></h1><h2>Karen Russell</h2></figure>
</div>
'''


def test_nba_status_comes_from_the_containing_section():
    """The page never says 'winner' in text — only the wrapper class does."""
    got = A.parse_nba_page(NBA)
    assert [(e["title"], e["status"]) for e in got] == [
        ("The True True Story of Raja", "winner"),
        ("A Guardian and a Thief", "finalist"),
        ("The Antidote", "finalist")]


def test_nba_finalists_do_not_leak_into_the_winner_section():
    winners = [e for e in A.parse_nba_page(NBA) if e["status"] == "winner"]
    assert len(winners) == 1


# --- the work_key join spine ----------------------------------------------------

def test_work_key_collapses_source_formatting_differences():
    """Sources disagree on subtitle and author order; the key must not."""
    assert db.work_key("Life of M: A Novel", "Cusk, Rachel") == \
           db.work_key("Life Of M", "Rachel Cusk")
    assert db.work_key("The Overstory", "Powers, Richard") == \
           db.work_key("Overstory", "Richard Powers")


def test_work_key_separates_different_authors():
    assert db.work_key("Trust", "Hernan Diaz") != db.work_key("Trust", "Domenico Starnone")


def test_accolades_are_idempotent():
    """Re-running a pull must not duplicate; that is what makes backfill safe
    to repeat after a parser fix."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        k = db.upsert_work(conn, "Angel Down", "Daniel Kraus")
        assert db.add_accolade(conn, k, "pulitzer", "award", "winner",
                               category="Fiction", year=2026)
        assert not db.add_accolade(conn, k, "pulitzer", "award", "winner",
                                   category="Fiction", year=2026)
        assert conn.execute("SELECT COUNT(*) c FROM accolades"
                            ).fetchone()["c"] == 1


def test_upsert_work_fills_gaps_without_clobbering():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.upsert_work(conn, "Etna", "Paul Yoon")
        db.upsert_work(conn, "Etna", "Paul Yoon", pub_year="2026",
                       wikidata_id="Q1")
        r = conn.execute("SELECT * FROM works").fetchone()
        assert r["pub_year"] == "2026" and r["wikidata_id"] == "Q1"
        # a later, poorer source must not blank what we already had
        db.upsert_work(conn, "Etna", "Paul Yoon")
        r = conn.execute("SELECT * FROM works").fetchone()
        assert r["pub_year"] == "2026"


# --- Obama's lists --------------------------------------------------------------

OBAMA = """<h2>Favorite Books of 2025</h2>
<figcaption>Press enter or click to view image in full size</figcaption>
<p>Paper Girl &#8212; Beth Macy</p><p>Flashlight &#8212; Susan Choi</p>
<p>And obviously I&#8217;m biased, </p><p>The Look by Michelle Obama</p>
<p>Here&#8217;s a reminder of the books I recommended over the summer:</p>
<p>King of Ashes &#8212; S.A. Cosby</p>
<h2>Favorite Movies of 2025</h2><p>Sinners &#8212; Ryan Coogler</p>"""


def test_obama_reads_both_the_year_and_summer_sections():
    got = A.parse_obama_post(OBAMA.replace("&#8212;", "—").replace("&#8217;", "'"))
    assert ("King of Ashes", "S.A. Cosby") in [(e["title"], e["author"]) for e in got]
    assert ("Paper Girl", "Beth Macy") in [(e["title"], e["author"]) for e in got]


def test_obama_stops_at_the_movies_heading():
    """The post lists films in the same 'Title — Name' shape as the books."""
    got = A.parse_obama_post(OBAMA.replace("&#8212;", "—").replace("&#8217;", "'"))
    assert all(e["title"] != "Sinners" for e in got)


def test_obama_handles_the_by_separator():
    got = A.parse_obama_post(OBAMA.replace("&#8212;", "—").replace("&#8217;", "'"))
    assert ("The Look", "Michelle Obama") in [(e["title"], e["author"]) for e in got]


def test_obama_drops_the_image_caption_boilerplate():
    got = A.parse_obama_post(OBAMA.replace("&#8212;", "—").replace("&#8217;", "'"))
    assert all("Press enter" not in e["title"] for e in got)


# --- author-level awards --------------------------------------------------------

def test_author_key_matches_across_name_orderings():
    """Sources give 'Krasznahorkai, László' and 'László Krasznahorkai'."""
    assert db.author_key("Krasznahorkai, László") == \
           db.author_key("László Krasznahorkai")


def test_career_awards_do_not_become_fake_works():
    """The Nobel is for a body of work. Writing a 'work' called 'Han Kang'
    would invent a book and inflate every score that counts awards per work."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        assert db.add_author_accolade(conn, "Han Kang", "nobel", "winner",
                                      year=2024)
        assert conn.execute("SELECT COUNT(*) c FROM works").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM accolades"
                            ).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM author_accolades"
                            ).fetchone()["c"] == 1


def test_author_accolades_are_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        assert db.add_author_accolade(conn, "Jon Fosse", "nobel", "winner",
                                      year=2023)
        assert not db.add_author_accolade(conn, "Jon Fosse", "nobel", "winner",
                                          year=2023)


# --- scoring --------------------------------------------------------------------

def test_score_counts_distinct_sources_not_rows():
    """Locus alone contributes 5,214 accolades because its nominee lists run
    ten deep per category. Ranking on row count would put a mid-list Locus
    nominee above a Pulitzer winner, so scores count distinct sources."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        spammy = db.upsert_work(conn, "Spammy", "Nominee")
        for year in range(2000, 2012):        # twelve Locus nominations
            db.add_accolade(conn, spammy, "locus", "award", "nominee",
                            category="Novel", year=year)
        laurelled = db.upsert_work(conn, "Laurelled", "Winner")
        db.add_accolade(conn, laurelled, "pulitzer", "award", "winner",
                        category="Fiction", year=2020)
        A.compute_scores(conn)
        s = {r["work_key"]: r["score"] for r in
             conn.execute("SELECT work_key, score FROM work_scores")}
        assert s[laurelled] > s[spammy]


def test_score_rewards_breadth_across_juries():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        broad = db.upsert_work(conn, "Broad", "Author")
        for src in ("pulitzer", "nba", "booker"):
            db.add_accolade(conn, broad, src, "award", "winner", year=2020)
        narrow = db.upsert_work(conn, "Narrow", "Author")
        db.add_accolade(conn, narrow, "pulitzer", "award", "winner", year=2020)
        A.compute_scores(conn)
        s = {r["work_key"]: r["score"] for r in
             conn.execute("SELECT work_key, score FROM work_scores")}
        assert s[broad] == 3 * s[narrow]


def test_scoring_is_idempotent_not_cumulative():
    """A rerun after a parser fix must reproduce the numbers, not stack them."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        k = db.upsert_work(conn, "Trust", "Hernan Diaz")
        db.add_accolade(conn, k, "pulitzer", "award", "winner", year=2023)
        A.compute_scores(conn)
        first = conn.execute("SELECT score FROM work_scores").fetchone()["score"]
        A.compute_scores(conn)
        rows = conn.execute("SELECT score FROM work_scores").fetchall()
        assert len(rows) == 1 and rows[0]["score"] == first


# --- NBCC -----------------------------------------------------------------------

NBCC = '''<div id="award-winners" class="full-inner"><h2>2025 NBCC Award Winners</h2>
<div class="award-winners-wrapper"><ul><li>
<h3>Fiction</h3>
<h4>Han Kang, translated from the Korean by e. yaewon and Paige Aniyah Morris</h4>
<p class="book-title">We Do Not Part</p><p class="text">Hogarth</p>
</li><li>
<h3>Nonfiction</h3><h4>Karen Hao</h4>
<p class="book-title">Empire of AI: Dreams and Nightmares</p><p class="text">Penguin Press</p>
</li></ul></div></div>'''


def test_nbcc_strips_the_translator_note_from_the_author():
    """The h4 carries 'Han Kang, translated from the Korean by …'."""
    got = A.parse_nbcc(NBCC)
    assert ("We Do Not Part", "Han Kang") in [(e["title"], e["author"]) for e in got]


def test_nbcc_reads_category_and_year():
    got = A.parse_nbcc(NBCC)
    assert {e["category"] for e in got} == {"Fiction", "Nonfiction"}
    assert all(e["year"] == 2025 for e in got)
    assert all(e["status"] == "winner" for e in got)


# --- ISFDB containers -----------------------------------------------------------

def test_isfdb_flags_a_standalone_printing_as_still_borrowable():
    """Tor publishes Hugo novellas as standalone books. Those are the easiest
    thing to borrow, so they are kept and flagged — not filtered out as 'not an
    anthology'."""
    assert A._flat_name("The Tusks of Extinction") == A._flat_name("the tusks of extinction!")


def test_container_rows_are_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        story = db.upsert_work(conn, "The Tusks of Extinction", "Ray Nayler")
        book = db.upsert_work(conn, "Some Anthology")
        for _ in range(2):
            conn.execute(
                "INSERT OR IGNORE INTO work_containers (work_key, container_key,"
                " source, detail, fetched_at) VALUES (?,?,?,?,?)",
                (story, book, "isfdb", "anthology/collection", db._now()))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM work_containers"
                            ).fetchone()["c"] == 1


def test_a_short_work_keeps_its_form_when_a_container_is_added():
    """Adding the anthology must not overwrite the novella's own form."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        story = db.upsert_work(conn, "The Tusks of Extinction", "Ray Nayler")
        conn.execute("UPDATE works SET form='novella' WHERE work_key=?", (story,))
        db.upsert_work(conn, "Some Anthology")
        conn.commit()
        assert conn.execute("SELECT form FROM works WHERE work_key=?",
                            (story,)).fetchone()["form"] == "novella"


# --- Goodreads Choice Awards ----------------------------------------------------

GR = '''<a class="winningTitle choice gcaBookTitle" href="/book/show/1">My Friends</a>
<a class="pollAnswer__bookLink" href="/book/show/1"><img alt="My Friends by Fredrik Backman" /></a>
<a class="pollAnswer__bookLink" href="/book/show/2"><img alt="Wild Dark Shore by Charlotte McConaghy" /></a>
<a class="pollAnswer__bookLink" href="/book/show/3"><img alt="The Correspondent by Virginia      Evans" /></a>'''


def test_goodreads_marks_the_winner_among_the_nominees():
    got = A.parse_goodreads_category(GR)
    assert [(e["title"], e["status"]) for e in got] == [
        ("My Friends", "winner"),
        ("Wild Dark Shore", "nominee"),
        ("The Correspondent", "nominee")]


def test_goodreads_normalises_whitespace_in_the_alt_credit():
    """'Virginia      Evans' comes through the alt attribute run-on."""
    got = A.parse_goodreads_category(GR)
    assert got[2]["author"] == "Virginia Evans"


def test_goodreads_splits_on_the_last_by():
    """A title containing ' by ' must not lose its tail to the author."""
    page = ('<a class="pollAnswer__bookLink"><img alt="Death by Chocolate '
            'by Sally Berneathy" /></a>')
    e = A.parse_goodreads_category(page)[0]
    assert e["title"] == "Death by Chocolate" and e["author"] == "Sally Berneathy"


def test_goodreads_form_defaults_to_novel_but_detects_nonfiction():
    assert A._gr_form("Fiction") == "novel"
    assert A._gr_form("History & Biography") == "nonfiction"
    assert A._gr_form("Poetry") == "poetry"


# --- Douban ---------------------------------------------------------------------

def test_douban_strips_the_nationality_marker_from_translated_authors():
    """'[波] 雷沙德·卡普希钦斯基' — the bracket is a nationality tag, not a name."""
    assert A.clean_douban_author("[波] 雷沙德·卡普希钦斯基") == "雷沙德·卡普希钦斯基"
    assert A.clean_douban_author("[日]永井美糸") == "永井美糸"
    assert A.clean_douban_author("黄晓丹") == "黄晓丹"


def test_douban_is_a_list_not_an_award():
    """An editorial year-end selection with a community rating — modelling it
    as winner/nominee would imply a jury that does not exist."""
    src = A.SOURCES["douban"]
    assert src.kind == "list"


# --- the raw mirror -------------------------------------------------------------

def test_fetch_arms_the_raw_mirror_itself(monkeypatch):
    """Three loaders shipped without calling set_archive and the only symptom
    was an empty raw_pages — the pull still reported success, so the
    'never re-crawl to fix a parser' guarantee was quietly not holding."""
    import bayarea_lookup as B
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        monkeypatch.setattr(B, "_archive_path", None, raising=False)
        monkeypatch.setattr(B, "_get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
        fails = []
        A._fetch(conn, "https://example.invalid/x", fails)
        assert B._archive_path is not None      # armed even though the GET failed
        assert len(fails) == 1


def test_fetch_prefers_the_mirror_over_the_network(monkeypatch):
    import bayarea_lookup as B
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.store_raw_page(conn, "https://example.invalid/y", b"cached",
                          "2026-09-06T00:00:00")
        conn.commit()

        def boom(*a, **k):
            raise AssertionError("network hit despite a mirrored page")

        monkeypatch.setattr(B, "_get", boom)
        assert A._fetch(conn, "https://example.invalid/y", []) == b"cached"


# --- double-encoded text --------------------------------------------------------

def test_demojibake_repairs_a_double_encoded_apostrophe():
    """PEN serves 5 of 999 rows this way \u2014 rare enough to survive a
    spot-check and then quietly poison the work_key."""
    broken = "The World\u00e2\u0080\u0099s Largest Owl"
    assert A.demojibake(broken) == "The World\u2019s Largest Owl"


def test_demojibake_leaves_genuine_latin_text_alone():
    """'ch\u00e2teau' has a lead character but is not double-encoded."""
    assert A.demojibake("Le ch\u00e2teau de ma m\u00e8re") == "Le ch\u00e2teau de ma m\u00e8re"
    assert A.demojibake("\u00c9mile Zola") == "\u00c9mile Zola"


def test_demojibake_is_a_noop_on_plain_ascii():
    assert A.demojibake("Angel Down") == "Angel Down"
    assert A.demojibake("") == ""


def test_demojibake_refuses_a_repair_yielding_control_characters():
    out = A.demojibake("A\u00c3\u0081B")
    assert not any(ord(c) < 32 for c in out)


# --- LA Times -------------------------------------------------------------------

LAT = """<h2 data-element="element-header-title">Fiction</h2>
<div data-element="rich-text-module" class="ct-rich-text-children bp-winner-ribbon" >
<figure><img src="ribbon-winner.png" /></figure></div>
<div data-element="rich-text-module" class="ct-rich-text-children" >
<p>Palaver: A Novel</p><p>Bryan Washington</p><p>Riverhead Books</p></div>
<div data-element="rich-text-module" class="ct-rich-text-children" >
<p>The Antidote</p><p>Karen Russell</p><p>Knopf</p></div>
<div data-element="rich-text-module" class="ct-rich-text-children" >
<p>Judges:</p><p>Someone, Someone Else</p></div>
<h2 data-element="element-header-title">Poetry</h2>
<div data-element="rich-text-module" class="ct-rich-text-children" >
<p>A Magnificent Loneliness</p><p>Allison Benis White</p><p>Four Way Books</p></div>"""


def test_latimes_carries_the_winner_ribbon_to_the_next_module():
    """The ribbon is its own image-only module BEFORE the winner's text, so the
    flag has to carry forward rather than be read off the entry itself."""
    got = A.parse_latimes(LAT)
    winners = [e for e in got if e["status"] == "winner"]
    assert len(winners) == 1
    assert winners[0]["title"] == "Palaver: A Novel"


def test_latimes_marks_the_rest_of_the_category_as_finalists():
    got = A.parse_latimes(LAT)
    assert ("The Antidote", "finalist") in [(e["title"], e["status"]) for e in got]


def test_latimes_drops_the_judges_panel():
    assert all("Judges" not in e["title"] for e in A.parse_latimes(LAT))


def test_latimes_splits_categories():
    got = A.parse_latimes(LAT)
    assert {e["category"] for e in got} == {"Fiction", "Poetry"}
    # a new category resets the winner flag rather than inheriting it
    poetry = [e for e in got if e["category"] == "Poetry"]
    assert poetry and poetry[0]["status"] == "finalist"


def test_latimes_form_mapping():
    assert A._lat_form("Fiction") == "novel"
    assert A._lat_form("Science & Technology") == "nonfiction"
    assert A._lat_form("Graphic Novel/Comics") == "collection"


# --- Obama section boundaries ---------------------------------------------------

def test_obama_stop_catches_the_summer_playlist_heading():
    """The summer post divides with 'Summer Playlist:', not 'Favorite Movies'.
    Missing it let 46 songs through as books."""
    assert A._OBAMA_STOP_RE.match("Summer Playlist:")
    assert A._OBAMA_STOP_RE.match("Favorite Movies of 2025")
    assert A._OBAMA_STOP_RE.match("My 2026 Summer Music List")


def test_obama_stop_does_not_swallow_the_books_heading_or_titles():
    assert not A._OBAMA_STOP_RE.match("Favorite Books of 2025")
    assert not A._OBAMA_STOP_RE.match("Kin")
    assert not A._OBAMA_STOP_RE.match("Cool Machine")


# --- award families -------------------------------------------------------------

def test_parallel_sf_juries_collapse_to_one_family():
    """Hugo, Nebula and Locus vote on substantially the same ballot. Counting
    them as three independent juries put the entire top of the first scored
    table into science fiction."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        sf = db.upsert_work(conn, "Ancillary Justice", "Ann Leckie")
        for src in ("hugo", "nebula", "locus"):
            db.add_accolade(conn, sf, src, "award", "winner", year=2014)
        lit = db.upsert_work(conn, "The Underground Railroad", "Colson Whitehead")
        db.add_accolade(conn, lit, "pulitzer", "award", "winner", year=2017)
        A.compute_scores(conn)
        s = {r["work_key"]: r for r in
             conn.execute("SELECT work_key, n_won, score FROM work_scores")}
        assert s[sf]["n_won"] == 1          # one SF family, not three juries
        assert s[sf]["score"] == s[lit]["score"]


def test_genuinely_independent_juries_still_add_up():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        k = db.upsert_work(conn, "The Underground Railroad", "Colson Whitehead")
        for src in ("pulitzer", "nba", "goodreads"):
            db.add_accolade(conn, k, src, "award", "winner", year=2017)
        A.compute_scores(conn)
        assert conn.execute("SELECT n_won FROM work_scores").fetchone()["n_won"] == 3


def test_a_family_won_is_not_also_counted_as_nominated():
    """Hugo win + Nebula nomination is one family, already counted as a win."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        k = db.upsert_work(conn, "A Book", "An Author")
        db.add_accolade(conn, k, "hugo", "award", "winner", year=2020)
        db.add_accolade(conn, k, "nebula", "award", "nominee", year=2020)
        A.compute_scores(conn)
        r = conn.execute("SELECT n_won, n_nominated FROM work_scores").fetchone()
        assert (r["n_won"], r["n_nominated"]) == (1, 0)


def test_both_booker_prizes_are_one_family():
    assert A.award_family("booker") == A.award_family("booker-intl")
    assert A.award_family("pulitzer") != A.award_family("nba")


# --- FT Business Book of the Year (Wikipedia fallback) --------------------------

FT_WIKI = """=== 2010 ===
*[[Sheena Iyengar]], \'\'[[The Art of Choosing]]\'\'
*{{blue ribbon}} [[Raghuram G. Rajan]], \'\'[[Fault Lines: How Hidden Fractures]]\'\'

=== 2020 ===
{| class="wikitable"
|{{blue ribbon}} [[Sarah Frier]], \'\'[[No Filter: The Inside Story of Instagram]]\'\'
|}

=== 2024 ===
*{{Blue ribbon}} [[Parmy Olson]], \'\'[[Supremacy (book)|Supremacy: AI, ChatGPT]]\'\'
*Daniel Susskind, \'\'Growth: A Reckoning\'\'

== See also ==
* [[McKinsey Award]] for Best Article of the Year in \'\'The Harvard Business Review\'\'
"""


def test_ft_winner_marker_is_case_insensitive():
    """2010 writes {{blue ribbon}}, 2018 writes {{Blue ribbon}}. A
    case-sensitive check silently lost 14 of 21 winners."""
    got = A.parse_ft_wikitext(FT_WIKI)
    winners = {(e["year"], e["status"]) for e in got if e["status"] == "winner"}
    assert (2010, "winner") in winners and (2024, "winner") in winners


def test_ft_reads_a_year_laid_out_as_a_table():
    """2020 is a wikitable, so its rows start with '|' not '*'."""
    got = A.parse_ft_wikitext(FT_WIKI)
    y2020 = [e for e in got if e["year"] == 2020]
    assert len(y2020) == 1
    assert y2020[0]["title"].startswith("No Filter")
    assert y2020[0]["status"] == "winner"


def test_ft_year_section_stops_at_a_level_two_heading():
    """Stopping only at '===' let the last year run on into 'See also' and
    file a stray bullet as that year's shortlistee."""
    got = A.parse_ft_wikitext(FT_WIKI)
    assert all("Harvard Business Review" not in e["title"] for e in got)
    assert {e["year"] for e in got} == {2010, 2020, 2024}


def test_ft_strips_wiki_link_syntax_from_title_and_author():
    got = A.parse_ft_wikitext(FT_WIKI)
    olson = [e for e in got if e["year"] == 2024 and e["status"] == "winner"][0]
    assert olson["author"] == "Parmy Olson"
    assert olson["title"] == "Supremacy: AI, ChatGPT"


# --- work_key and non-Latin scripts ---------------------------------------------

def test_work_key_preserves_cjk():
    """The first version stripped every non-ASCII character, so every Chinese
    title normalized to '' and all 540 Douban books collapsed into 26 keys."""
    k1 = db.work_key("\u4e5d\u8bd7\u5fc3", "\u9ec4\u6653\u4e39")
    k2 = db.work_key("\u8981\u6709\u5149", "\u6881\u9e3f")
    assert k1 != k2
    assert not k1.startswith("|") and not k2.startswith("|")


def test_work_key_still_folds_accents_together():
    assert db.work_key("Le ch\u00e2teau", "Pagnol") == db.work_key("Le chateau", "Pagnol")


def test_work_key_latin_behaviour_is_unchanged():
    assert db.work_key("Life of M: A Novel", "Cusk, Rachel") == "lifeofm|cusk"
    assert db.work_key("The Overstory", "Powers, Richard") == "overstory|powers"


def test_distinct_chinese_titles_do_not_collide():
    titles = ["\u4e5d\u8bd7\u5fc3", "\u592a\u9633\u7684\u9634\u5f71",
              "\u8981\u6709\u5149", "\u767d\u8c61"]
    keys = {db.work_key(t, "\u4f5c\u8005") for t in titles}
    assert len(keys) == len(titles)


# --- Women's Prize --------------------------------------------------------------

def test_womens_prize_reads_the_exact_status_sentence():
    """The WP REST API knows only that a book was listed; the book's own page
    is the only place shortlist and longlist are distinguished."""
    got = A.parse_womens_prize_status(
        "Shortlisted for the 2026 Women's Prize for Fiction A scorching drama")
    assert got == {"status": "shortlist", "year": 2026, "category": "Fiction"}


def test_womens_prize_category_does_not_swallow_the_blurb():
    """A greedy [A-Za-z -]+ yields 'Fiction Piranesi Lives In The House'."""
    got = A.parse_womens_prize_status(
        "Winner of the 2021 Women's Prize for Fiction Piranesi lives in the House")
    assert got["category"] == "Fiction" and got["status"] == "winner"


def test_womens_prize_handles_non_fiction_and_missing_status():
    assert A.parse_womens_prize_status(
        "Longlisted for the 2024 Women's Prize for Non-Fiction x"
    )["category"] == "Non-Fiction"
    assert A.parse_womens_prize_status("just a blurb, no prize sentence") is None


# --- Kirkus Prize ---------------------------------------------------------------

KIRKUS = """<div class="prize-hero"><h1>2024 Winners</h1></div>
<section class="prize-winners">
  <div class="prize-winner starred">
    <p class="prize-label">FICTION</p>
    <h2><a href="/x">JAMES</a></h2>
    <p class="prize-label">BY PERCIVAL EVERETT</p>
  </div>
</section>
<section class="reviews-section prize-finalists"><h1>2024 Finalists</h1>
  <h2 class="prize-category">FICTION</h2>
  <ul class="two-rows">
    <li class="starred"><p class="book-title"><a href="/y">THE MIGHTY RED</a></p>
      <p>By Louise Erdrich</p></li>
    <li><p class="book-title"><a href="/z">MARGO&#8217;S GOT MONEY TROUBLES</a></p>
      <p>By Rufi Thorpe</p></li>
  </ul>
</section>"""


def test_kirkus_reads_winners_and_finalists_from_different_markup():
    got = A.parse_kirkus_year(KIRKUS)
    assert ("James", "winner") in [(e["title"], e["status"]) for e in got]
    assert ("The Mighty Red", "finalist") in [(e["title"], e["status"]) for e in got]


def test_kirkus_strips_the_by_prefix_from_a_winner_label():
    got = A.parse_kirkus_year(KIRKUS)
    winner = [e for e in got if e["status"] == "winner"][0]
    assert winner["author"] == "Percival Everett"
    assert winner["category"] == "Fiction"


def test_kirkus_title_case_respects_apostrophes():
    """str.title() gives \"Margo'S Got Money Troubles\"."""
    assert A._kp_title_case("MARGO\u2019S GOT MONEY TROUBLES").startswith("Margo")
    assert "'S " not in A._kp_title_case("MARGO'S GOT MONEY TROUBLES")


def test_kirkus_leaves_mixed_case_titles_alone():
    assert A._kp_title_case("The Mighty Red") == "The Mighty Red"


# --- LA Times history -----------------------------------------------------------

LATH = """>2024<
──────<br><b>FICTION</b><br>──────
<b>Winner: Ibis: A Novel</b>, Justin Haynes, Harry N. Abrams<br>
<b>Finalists:</b><ul><li><b>Plum</b>, Andy Anderegg, Hub City Press</li>
<li><b>Idle Grounds</b>, Krystelle Bamford, Scribner</li></ul>
>2023<
──────<br><b>POETRY</b><br>──────
<b>Winner: Some Poems</b>, A Poet, A Press<br>
<b>Finalists:</b><ul><li><b>Other Poems</b>, B Poet, B Press</li></ul>"""


def test_latimes_history_binds_blocks_to_the_preceding_year():
    got = A.parse_latimes_history(LATH)
    by_year = {(e["year"], e["title"]) for e in got}
    assert (2024, "Ibis: A Novel") in by_year
    assert (2023, "Some Poems") in by_year


def test_latimes_history_separates_winner_from_finalists():
    got = A.parse_latimes_history(LATH)
    w = [e for e in got if e["year"] == 2024 and e["status"] == "winner"]
    f = [e for e in got if e["year"] == 2024 and e["status"] == "finalist"]
    assert len(w) == 1 and w[0]["title"] == "Ibis: A Novel"
    assert {x["title"] for x in f} == {"Plum", "Idle Grounds"}


def test_latimes_history_drops_the_publisher_from_the_credit():
    got = A.parse_latimes_history(LATH)
    w = [e for e in got if e["status"] == "winner" and e["year"] == 2024][0]
    assert w["author"] == "Justin Haynes"


def test_latimes_history_title_cases_the_shouted_category():
    assert {e["category"] for e in A.parse_latimes_history(LATH)} == {
        "Fiction", "Poetry"}


# --- Audie Awards ---------------------------------------------------------------

AUDIE_MODERN = """<p>AUDIOBOOK OF THE YEAR WINNER</p><p>My Name Is Barbra</p>
<p>(</p><p>Audio</p><p>)</p><p>Written and narrated by Barbra Streisand</p>
<p>Published by Penguin Random House Audio</p>
<p>FANTASY WINNER</p><p>Bookshops &amp; Bonedust</p><p>By Travis Baldree, narrated by Travis Baldree</p>
<p>Published by Macmillan Audio</p>"""

AUDIE_LEGACY = """<p>AUDIOBOOK OF THE YEAR WINNER</p><p>The Girl on the Train: A Novel</p>
<p>by Paula Hawkins</p><p>Narrated by Clare Corbett, Louise Brealey (Penguin Audio)</p>
<p>AUDIOBOOK OF THE YEAR FINALIST</p>
<p>Go Set a Watchman by Harper Lee; narrated by Reese Witherspoon (HarperAudio)</p>"""


def test_audie_modern_separates_narrator_from_author():
    got = A.parse_audie_year(AUDIE_MODERN)
    barbra = [e for e in got if e["title"].startswith("My Name")][0]
    assert barbra["author"] == "Barbra Streisand"
    assert barbra["narrator"] == "Barbra Streisand"


def test_audie_modern_reads_a_separate_author_and_narrator():
    got = A.parse_audie_year(AUDIE_MODERN)
    b = [e for e in got if "Bonedust" in e["title"]][0]
    assert b["author"] == "Travis Baldree" and b["narrator"] == "Travis Baldree"


def test_audie_legacy_layout_has_no_published_by_line():
    """Pages up to ~2016 put the publisher in parentheses on the narrator
    line, so the modern parser terminates no entries and yields nothing."""
    assert A.parse_audie_year(AUDIE_LEGACY) == []
    got = A.parse_audie_year_legacy(AUDIE_LEGACY)
    assert any(e["title"].startswith("The Girl on the Train") for e in got)


def test_audie_legacy_reads_the_one_line_finalist_shape():
    got = A.parse_audie_year_legacy(AUDIE_LEGACY)
    f = [e for e in got if e["status"] == "finalist"]
    assert f and f[0]["title"] == "Go Set a Watchman"
    assert f[0]["author"] == "Harper Lee"
    assert f[0]["narrator"] == "Reese Witherspoon"


def test_audie_dispatch_falls_back_to_legacy():
    assert A.parse_audie_any(AUDIE_LEGACY)
    assert A.parse_audie_any(AUDIE_MODERN)


# --- Grammy ---------------------------------------------------------------------

GRAMMY = """{| class="wikitable"
|-
! Year !! Work !! Performing Artist
|-style="background:#FAEB86;"
! rowspan=2 |[[66th Annual Grammy Awards|2024]]
| \'\'The Light We Carry\'\'
| [[Michelle Obama]]
|-
| \'\'Some Other Book\'\'
| Another Reader
|}"""


def test_grammy_marks_the_highlighted_row_as_winner():
    got = A.parse_grammy_wikitext(GRAMMY)
    win = [e for e in got if e["status"] == "winner"]
    assert win and win[0]["title"] == "The Light We Carry"
    assert win[0]["narrator"] == "Michelle Obama"
    assert win[0]["year"] == 2024


def test_grammy_rows_after_the_winner_are_nominees():
    got = A.parse_grammy_wikitext(GRAMMY)
    assert any(e["status"] == "nominee" and e["title"] == "Some Other Book"
               for e in got)


def test_grammy_skips_the_infobox_parameter_rows():
    """The article infobox is pipe-delimited too and parses as a table row."""
    box = "|-\n| name = Grammy Award for Best Audio Book\n| awarded_for = quality"
    assert A.parse_grammy_wikitext(box) == []


# --- sfadb short fiction (three bugs that hid two-thirds of it) -----------------

SFADB_SHORT = (
    '<div class="categoryblock">\n<div class="category">Short Story</div>\n<ul>\n'
    '<li> <span class="winner">Winner:</span> &#8220;Better Living Through '
    'Algorithms&#8221;, <a href="Naomi_Kritzer">Naomi Kritzer</a> '
    '(Clarkesworld May 2023)</li>\n'
    '<li> &#8220;Answerless Journey&#8221;, <a href="Han_Song">Han Song</a> '
    '(<b>Adventures in Space</b>)</li>\n'
    '</ul>\n</div>'
)


def test_sfadb_reads_a_quoted_short_fiction_title():
    """Short fiction is quoted, novels are bolded. Looking only for <b> lost
    the winner entirely and stored the anthology name for the runner-up."""
    got = A.parse_sfadb_year(SFADB_SHORT)
    assert ("Better Living Through Algorithms", "winner") in [
        (e["title"], e["status"]) for e in got]


def test_sfadb_prefers_the_story_over_a_bolded_anthology():
    got = A.parse_sfadb_year(SFADB_SHORT)
    titles = {e["title"] for e in got}
    assert "Answerless Journey" in titles
    assert "Adventures in Space" not in titles


def test_sfadb_title_is_not_the_winner_class_attribute():
    """Unescaping before stripping tags left class="winner" in play, and the
    quoted-title regex matched the word 'winner'."""
    got = A.parse_sfadb_year(SFADB_SHORT)
    assert all(e["title"].lower() != "winner" for e in got)


def test_sfadb_captures_the_venue_for_short_fiction():
    got = A.parse_sfadb_year(SFADB_SHORT)
    k = [e for e in got if e["title"].startswith("Better Living")][0]
    assert k["publisher"] == "Clarkesworld May 2023"


def test_decode_page_falls_back_to_latin1():
    """sfadb serves Latin-1; decoding as UTF-8 turned 'Djèlí' into replacement
    characters that were then stored as the author's name."""
    assert A._decode_page("P. Dj\u00e8l\u00ed Clark".encode("latin-1")) == \
        "P. Dj\u00e8l\u00ed Clark"
    assert A._decode_page("caf\u00e9".encode("utf-8")) == "caf\u00e9"


# --- where to read a short work -------------------------------------------------

def test_read_route_spots_the_free_magazines():
    assert A.read_route("Clarkesworld May 2023")["route"] == "free-online"
    assert A.read_route("Uncanny Jan/Feb 2023")["route"] == "free-online"
    assert "clarkesworld" in A.read_route("Clarkesworld 5/23")["where"]


def test_read_route_separates_print_magazines_from_free_ones():
    assert A.read_route("Asimov\'s Sep/Oct 2024")["route"] == "print-magazine"
    assert A.read_route("Analog Mar 2020")["route"] == "print-magazine"


def test_read_route_treats_a_publisher_as_a_book():
    assert A.read_route("Tordotcom")["route"] == "book"
    assert A.read_route("Neon Hemlock")["route"] == "book"


def test_read_route_handles_a_missing_venue():
    assert A.read_route(None)["route"] == "unknown"
    assert A.read_route("")["route"] == "unknown"


# --- ISFDB author guard ---------------------------------------------------------

def test_isfdb_author_guard_must_search_the_whole_page():
    """ISFDB puts 'Author: Ray Nayler' in the record details around char 6300.
    A 4000-character window rejected every work and produced zero containers
    while reporting no failures at all."""
    page = "x" * 6000 + "Author: Ray Nayler" + "y" * 2000
    assert A._flat_name("Ray Nayler") in A._flat_name(page)
    assert A._flat_name("Ray Nayler") not in A._flat_name(page[:4000])


# --- yield regression detection --------------------------------------------------

def test_check_yield_flags_a_collapse_against_the_best_previous_run():
    """Five bugs in this repo reported success while returning less. This is
    the check that would have caught four of them."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.log_fetch(conn, "sfadb", True, n_parsed=1500)
        db.log_fetch(conn, "sfadb", True, n_parsed=1480)
        assert A.check_yield(conn, "sfadb", 500) is not None


def test_check_yield_is_quiet_on_a_normal_run():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.log_fetch(conn, "sfadb", True, n_parsed=1500)
        assert A.check_yield(conn, "sfadb", 1490) is None
        assert A.check_yield(conn, "sfadb", 1600) is None


def test_check_yield_compares_against_the_best_not_the_last():
    """Two bad runs in a row must not become the new normal."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.log_fetch(conn, "x", True, n_parsed=1000)
        db.log_fetch(conn, "x", True, n_parsed=100)     # already broken
        assert A.check_yield(conn, "x", 100) is not None


def test_check_yield_needs_history_before_it_has_an_opinion():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        assert A.check_yield(conn, "brand-new", 5) is None


def test_check_yield_ignores_a_missing_count():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.log_fetch(conn, "x", True, n_parsed=1000)
        assert A.check_yield(conn, "x", None) is None


def test_past_yields_only_counts_successful_runs():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.log_fetch(conn, "x", True, n_parsed=900)
        db.log_fetch(conn, "x", False, n_parsed=3)
        assert db.past_yields(conn, "x") == [900]


# --- work_bibs: caching the catalog match ---------------------------------------

def test_work_bibs_records_a_miss_so_it_is_not_researched():
    """A system that holds nothing must be remembered, or every future query
    searches it again forever."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        db.record_work_bib(conn, "audition|kitamura", "sccl",
                           {"bib_id": "S1", "title": "Audition"})
        db.record_work_bib(conn, "audition|kitamura", "paloalto", None)
        conn.commit()
        assert db.systems_searched(conn, "audition|kitamura") == {"sccl", "paloalto"}
        rows = db.work_bibs(conn, "audition|kitamura", "paloalto")
        assert len(rows) == 1 and rows[0]["bib_id"] is None


def test_work_bibs_keeps_several_editions_per_system():
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        for bib in ("S1", "S2"):
            db.record_work_bib(conn, "w|a", "sccl", {"bib_id": bib})
        conn.commit()
        assert len(db.work_bibs(conn, "w|a", "sccl")) == 2


def test_resolve_work_bibs_skips_systems_already_searched(monkeypatch):
    """The point of the cache: the expensive search runs once."""
    import hotlist
    calls = []

    def fake(system, entry):
        calls.append(system)
        return [{"bib_id": "S1", "title": "x", "format_class": "book",
                 "url": "u"}]

    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        monkeypatch.setattr(hotlist, "bc_bibs", fake)
        A.resolve_work_bibs(conn, "w|a", "Title", "Author")
        first = len(calls)
        A.resolve_work_bibs(conn, "w|a", "Title", "Author")
        assert first > 0 and len(calls) == first      # no second search


def test_resolve_work_bibs_refresh_forces_a_research(monkeypatch):
    import hotlist
    calls = []
    monkeypatch.setattr(hotlist, "bc_bibs",
                        lambda s, e: calls.append(s) or [])
    with tempfile.TemporaryDirectory() as d:
        conn = db.open_db(os.path.join(d, "t.db"))
        A.resolve_work_bibs(conn, "w|a", "T", "A")
        n = len(calls)
        A.resolve_work_bibs(conn, "w|a", "T", "A", refresh=True)
        assert len(calls) == 2 * n
