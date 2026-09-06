# shelfwalk — working notes

Tracks a curated toddler want-list against Bay Area library shelves. Formerly
`peorialib`; **Peoria is retired** (frozen snapshot only — don't propose
re-scrapes or browser work there). The live path is `bayarea_lookup.py` →
SQLite → `report.py`.

## Ground rules

- `uv` for everything: `uv run bayarea_lookup.py`, `uv run pytest -q`,
  `uv add <pkg>`. Never `pip`.
- **The report `.md` files are generated artifacts** (`bayarea.md`, `titles.md`,
  `sccl.md`, `sjpl.md`, `mountainview.md`, `linkplus.md`, and the frozen Peoria
  set). Never hand-edit them; change `report.py` and run `uv run report.py
  --write`. `README.md`, this file and `docs/` are hand-written.
- `shelfwalk.db` is the source of truth and is gitignored. The schema in
  `catalog_db.py` recreates it; `open_db()` migrates missing columns.
- Commit/push only when asked.

## Where things live

| Concern | Place |
|---|---|
| Search, matching, availability, enrichment | `bayarea_lookup.py` |
| Schema + all SQL | `catalog_db.py` |
| Every markdown renderer | `report.py` |
| Want-list data | `wantlist_{en,zh,fr}.json`, `wantlist_exclude.json` |
| Favorite branches | `report.py:FAVORITES` |
| Hot new releases: watch + auto-hold | `hotlist.py`, `hotlist.json` |
| What the hold leg still needs | `docs/hold-recon.md` |
| Awards / best-of corpus | `acclaim.py` (sources registered in `SOURCES`) |
| Sources that need a Chrome pass | `docs/harvesting.md`, `tools/collector.py` |

## Matching invariants (each one is a bug that already bit)

Every rule below has a test in `test_bayarea_lookup.py`. If a change makes one
fail, the rule is probably right and the change is wrong.

- A candidate's **subtitle is part of its identity** — the bare title never
  scores alone (BiblioCommons files series volumes as `Grumpy Monkey` +
  subtitle `Too Many Bugs`).
- Only **descriptive** subtitles may be dropped for stem matching ("a
  lift-the-flap book"), never volume names. Parallel titles (`= Tren de carga`)
  are exempt.
- Extra same-work editions need ≥ `EDITION_MIN_RATIO` (0.95); short-suffix
  spinoffs live in 0.90–0.95 ("…Caterpillar's Eid", "Dragons Love Tacos 2").
- Editions must share the primary's language; foreign records go through the
  **translation** route, which requires the record's own stated original
  (`Translation of:` note / uniform title) to name the want.
- Pinyin/CJK comparisons need ≥ `PINYIN_MIN_RATIO` (0.85) — syllable streams
  blur ('zhe shi wo de' vs *That's Not My Hat*).
- Movies and music are never candidates; digital editions are tracked but
  carry no shelf state.
- WebPAC queries: fold diacritics, join apostrophes (`can't`→`cant`), drop a
  mid-query `not` (it's a boolean operator), CJK → pinyin (the server 502s).

## Data safety nets

- Every HTTP response is mirrored into `raw_pages`. **Before re-scraping to
  debug a parser, check the mirror** — `db.get_raw_page(conn, url)`.
- Re-lookups supersede wholesale: `replace_remote_editions` +
  `latest_remote_availability` (newest scrape per (system, record) joined to
  currently-matched bibs), so corrected matches leave no stale footprint.
- Systems run in parallel threads, one connection each (WAL); `_pace()` spaces
  requests per host — SCCL and SJPL share the BiblioCommons gateway and it
  403s uncoordinated threads.

## Hot list — a different problem from the want-list

`hotlist.py` watches a few adult new releases (`hotlist.json`) and queues for
them. It is deliberately *not* wired into the want-list path, because the two
answer different questions: the want-list asks "is it on a shelf this morning",
the hot list asks "where in the queue will I be on publication day".

Three rules, each one learned from the Taipei Story lookup on 2026-09-05:

- **Match on ISBN, never on the title.** San José catalogued it as `Taipei
  Story (Deluxe Limited Edition)`, which scores 0.667 against the want-list
  matcher and is discarded as a miss — losing the one system whose queue was
  worth joining. `_entry_matches` is deliberately looser than
  `bayarea_lookup.pick_all`; here a false negative (no hold) costs far more
  than a false positive (a stray edition in a report).
- **"Holdable" is not the trigger, and holds-per-copy is not a boolean.** The
  record was holdable with 30 of 38 copies still on order. The event worth
  catching is the bib *appearing*; the number worth ranking on is holds/copy,
  which read 0.36 / 1.58 / 4.00 across three systems on the same day.
- **Rank only what you can actually join.** San José's Lucky Day shelf reads 1
  hold on 28 copies — a 0.04/copy queue that accepts no holds at all. Both
  `status` and `plan_holds` check `holdable` before ranking; a test pins it.

Placing holds is opt-in twice over: credentials must be in the login keyring
(never the repo, never a log), and `SHELFWALK_PLACE_HOLDS=1` must be set in the
systemd unit. The `UNIQUE(slug, system, bib_id)` constraint on `hot_holds` is
what stops a bug re-queueing a title; the batch cap refuses the whole run
rather than placing part of it.

## Acclaim corpus — sourcing rules

`acclaim.py` builds the "what is worth reading" half that `hotlist.py` and the
want-list then locate on a shelf. Three rules it is built around:

- **Original sources; Wikipedia/Wikidata is a fallback and a cross-check, never
  the primary.** Measured 2026-09-06: Wikidata holds 818 Booker nominees but
  21 Pulitzer finalists (there are ~200), 6 Women's Prize, 1 NBCC. It is fine
  for winners and useless for shortlists, which is most of the value.
- **Raw first, always.** Scripted fetches mirror into `raw_pages` via
  `bayarea_lookup._get`; browser harvests land in `harvest/*.json`. Re-parsing
  must never mean re-crawling — and for the browser tier, re-crawling means
  driving Chrome by hand.
- **One writer at a time.** Every source writes the same SQLite file, and a
  concurrent backfill produces `database is locked` mid-scrape. `acclaim.sh`
  takes a `flock`; don't run two pulls in parallel.

Adding a source should be one parser plus one `Source(...)` line. If it needs
more, the framework is wrong rather than the source.

Two modelling decisions that are easy to get wrong:

- **Career awards are not work awards.** The Nobel in Literature is given to a
  person for a body of work, as are the SFWA Grand Master and most lifetime
  honours. Those go to `author_accolades`, never `works` — inventing a book
  called "Han Kang" would fabricate a work *and* inflate every score that
  counts distinct awards per work.
- ⭐ **`work_key` is Unicode-aware, and must stay that way.** Stripping to
  `[a-z0-9]` folds every CJK title to the empty string — 540 Douban books
  became 26 keys, silently. Fold accents first, then keep `\w` with the
  UNICODE flag.
- **Scores count distinct sources, never rows.** Locus alone contributes 5,214
  accolades because its nominee lists run ten deep in every category; ranking
  on row count puts a mid-list Locus nominee above a Pulitzer winner. Breadth
  across independent juries is the signal. `compute_scores` is a full
  recompute, never incremental, so a rerun after a parser fix reproduces the
  numbers instead of stacking on them.

**The browser tier cannot run on a timer.** `pulitzer.org` 403s `urllib` *and*
`curl` with identical headers — that is TLS fingerprinting, and no header
spoofing gets past it — while NYT/WSJ need Darren's login. `fetch()` from
inside a real tab works, POSTing to `tools/collector.py`. Two traps, both of
which cost time: Chrome's Private Network Access silently drops a POST to
127.0.0.1 unless the response carries `Access-Control-Allow-Private-Network`,
and six sequential in-page fetches blow the 45s CDP timeout where a
`Promise.all` over the same six does not.
