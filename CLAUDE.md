# shelfwalk — working notes

Three tools on one SQLite store: the **want-list** (a toddler's books, located on
Bay Area shelves each morning), the **hot list** (a few adult new releases,
watched for their hold queues) and the **acclaim corpus** (award and best-of
lists, joined to the catalogs). Peoria is retired: a frozen snapshot, no
re-scrapes or browser work there.

## Ground rules

- `uv` for everything: `uv run bayarea_lookup.py`, `uv run pytest -q`,
  `uv add <pkg>`. Never `pip`.
- **Report `.md` files are generated** (`bayarea.md`, `titles.md`, `sccl.md`,
  `sjpl.md`, `mountainview.md`, `linkplus.md`, and the Peoria set); each carries
  the `AUTO-GENERATED` banner. Change `report.py` and run
  `uv run report.py --write`. `README.md`, this file and `docs/` are hand-written.
- `shelfwalk.db` is the source of truth and is gitignored; `catalog_db.py`
  recreates the schema and `open_db()` migrates missing columns.
- Commit/push only when asked. `refresh.sh` commits only the generated
  reports whenever its daily timer runs, but with `SHELFWALK_PUSH=1` (set in
  the supplied unit) it pushes every unpushed commit on the current branch.

## Where things live

| Concern | Place |
|---|---|
| Want-list search, matching, availability, enrichment | `bayarea_lookup.py` |
| Schema and shared queries | `catalog_db.py` |
| Every markdown renderer; want-list favourite branches (`FAVORITES`) | `report.py` |
| Want-list data | `wantlist_{en,zh,fr}.json`, `wantlist_exclude.json` |
| Hot list: watch + holds | `hotlist.py`, `hotlist.json`; open work in `docs/hold-recon.md` |
| Acclaim CLI, `SOURCES` registry, scoring, shelf join | `acclaim.py` |
| Children's awards and age-reviewed recommendations | `children.py`, `sources/children_awards.py`, `data/children/`, `children-books.md` |
| One adapter per awarding body | `sources/<name>.py`; inventory in `docs/sources.md` |
| Transports, mirror, text repair, yield guard | `acclaim_core.py` |
| Browser-tier harvests | `docs/harvesting.md`, `tools/collector.py`, `harvest/` |
| Mountain View fallback and bulk availability APIs | `docs/availability-research.md` |
| Scheduled jobs | `refresh.sh`, `hotwatch.sh`, `acclaim.sh`, `systemd/` |

## Catalog systems

- `sccl`, `sjpl` and `paloalto` are BiblioCommons (gateway JSON API); `mvpl` and
  `linkplus` are Innovative WebPACs (HTML). `paloalto` is used only by the
  acclaim shelf join.
- ⛔ **Mountain View is skipped** (`bayarea_lookup.SKIPPED_SYSTEMS`). Its WebPAC
  refuses any search not launched from its own search page (since 2026-09-12),
  and robots.txt on it and on its Vega catalog disallows crawlers. Don't forge
  a Referer or scrape Vega; check a title by hand in the browser. Record links
  still load.
- A system that fails `MAX_CONSECUTIVE_FAILURES` titles in a row is dropped for
  the run, so one blocked catalog can't run the refresh into its timeout.
- `report.stale_systems` compares successful snapshots to wall-clock time.
  Every observation expires after 20h, even within an otherwise fresh system.
  Old shelf marks never take a title off the hold list. Tests pass an explicit
  `now` to report generation when rendering historical fixtures.
- `mvpl_linkplus` is a report-only view of LINK+ rows owned by Mountain View,
  never a new catalog client or database system. It keeps union record links
  and source dates; missing titles are unknown. Its fresh copies count as local.
- Threads run one per system with a connection each (WAL); `_pace()` spaces
  requests per host because SCCL, SJPL and Palo Alto share the gateway and it
  403s uncoordinated bursts.

## Discovery cache and language filter

Tested in `test_lookup_cache.py` (language exclusion also in
`test_bayarea_lookup.py`).

- Spanish (`spa`), Japanese (`jpn`) and French (`fre`) records are excluded,
  including primary, audio and digital candidates. Extra translations are
  Chinese only. `prune_excluded_editions()` removes cached matches before
  polling/enrichment; observation history remains archived. Do not force all
  discovery caches to expire merely to remove excluded editions.
- SCCL/SJPL/LINK+ discovery is cached for seven days, including misses.
  `remote_bibs.checked_at` is the discovery date; status-only refreshes must
  not move it or replace enriched editions. `query_key` fingerprints matching
  inputs and has a version to bump after significant matching changes.
- Deduplicate successful availability lookups by bib within each system's run,
  never across runs. `--rediscover` bypasses discovery reuse. A failing cached
  bib invalidates its mapping for the next run without publishing partial data.

## Want-list matching invariants

Each rule has a test in `test_bayarea_lookup.py`; if a change breaks one, the
rule is probably right.

- A candidate's **subtitle is part of its identity** — the bare title never
  scores alone (BiblioCommons files series volumes as `Grumpy Monkey` +
  subtitle `Too Many Bugs`).
- Only **descriptive** subtitles ("a lift-the-flap book") may be dropped for
  stem matching, never volume names. Parallel titles (`= Tren de carga`) are
  exempt.
- Extra same-work editions need ≥ `EDITION_MIN_RATIO` (0.95); short-suffix
  spinoffs sit in 0.90–0.95 ("…Caterpillar's Eid", "Dragons Love Tacos 2").
- Editions share the primary's language; foreign records go through the
  **translation** route, which requires the record's own stated original
  (`Translation of:` note / uniform title) to name the want.
- A new title "from/with <wanted book>" is a spin-off, not an edition. Reject
  these primary matches and rediscover affected cached matches without expiring
  the entire discovery cache.
- Pinyin/CJK comparisons need ≥ `PINYIN_MIN_RATIO` (0.85) — syllable streams
  blur ('zhe shi wo de' vs *That's Not My Hat*).
- Movies and music are never candidates; digital editions are tracked but carry
  no shelf state.
- WebPAC queries: fold diacritics, join apostrophes (`can't`→`cant`), drop a
  mid-query `not` (a boolean operator), CJK → pinyin (the server 502s).
- BiblioCommons format codes are classified explicitly in `_bc_format_class`;
  an unknown code is `"other"`, never `"book"`. `test_mirror.py` fails on any
  mirrored code that isn't listed.

## Data safety nets

- Every HTTP response is mirrored into `raw_pages`. **Check the mirror before
  re-scraping to debug a parser** — `db.get_raw_page(conn, url)`.
- Re-lookups supersede wholesale: `replace_remote_editions` +
  `remote_availability_snapshots` + `latest_remote_availability` (completed
  snapshot per (system, record), joined to currently matched bibs), so an
  empty success clears old copies. Fetch all editions first, then publish
  matches, editions, snapshot and items in one transaction; a failed edition
  must not publish a partial result.
  `resolve_work_bibs` replaces a system's cached acclaim matches the same way.
- A failed search is never a recorded miss: `shelf_candidates` raises when
  `bc_bibs` reports an error, so `work_bibs` doesn't cache "not held".

## Hot list

`hotlist.py` answers "where will I be in the queue on publication day", a
different question from the want-list's "is it on a shelf this morning", so the
two stay separate.

- **Match on ISBN first.** `_entry_matches` accepts a title that merely contains
  the watched one (SJPL catalogues *Taipei Story* as `Taipei Story (Deluxe
  Limited Edition)`), because a missed hold costs more than a stray edition.
- **Trigger on the bib appearing, rank on holds per copy.** A record is often
  holdable while most copies are still on order.
- **Rank only what can be joined.** SJPL "Lucky Day" copies take no holds;
  `status` and `plan_holds` check `holdable` first.
- Placing holds is opt-in three times: `auto_hold` on the watchlist entry,
  credentials in the login keyring (never the repo or a log), and
  `SHELFWALK_PLACE_HOLDS=1` in the systemd unit. The placers
  are unimplemented until the endpoints are captured (`docs/hold-recon.md`).
  `UNIQUE(slug, system, bib_id)` on `hot_holds` stops a re-queue; the batch cap
  refuses a whole run rather than placing part of it.

## Children's awards

Workflow and coverage: `docs/children-awards.md`.

- `children.py pull` rebuilds from saved source bytes; `--refresh` downloads
  current archives. `find`/`report` stay offline.
- `find --age` uses reviewed suggestions; `--candidates` requires
  source-supplied age guidance. Award status never implies toddler suitability.
- Keep winner/honor/nominee/shortlist/commended stages and
  award-year/publication-year distinctions.
- Importing the archive must not add thousands of books to library
  availability polling. `children-books.md` is generated by `report.py` from
  `data/children/recommendations.json` and the archive.
- Popular acclaim uses dated evidence in `data/children/popularity.json` and
  saved Goodreads Choice records. Keep age-fit groups ahead of popularity,
  count each signal family once, and label unknown coverage explicitly.
  Historical bestseller claims are not current charts. `pull --refresh` does
  not refresh manually verified rating snapshots.
- `preferences.json` saves user-reported favorites and tentative theme
  connections. Personal affinity adds at most one ranking point within age-fit
  groups, never qualifies a book for `--popular`, and does not add favorites to
  availability polling.

## Acclaim corpus

- **Original sources first.** Wikipedia/Wikidata is a fallback and
  cross-check: it has winners but few shortlists (21 Pulitzer finalists of ~200
  as of 2026-09). `grammy` and `ft-business` are the only Wikipedia-sourced
  sources, and every row says so in `detail`.
- **Raw first.** Scripted fetches mirror into `raw_pages`; browser harvests land
  in `harvest/`. Re-parsing never means re-crawling.
- **One writer at a time.** `acclaim.sh` takes a `flock`; don't run two pulls in
  parallel.
- **Adding a source** is one module in `sources/` plus one `Source(...)` line in
  `acclaim.py`.
- ⭐ **A source that returns less than before has broken.** `check_yield`
  compares each run's `n_parsed` with the best of the last ten successful runs
  and `pull` exits 3 on a collapse. Every loader logs `n_parsed` on success. Use `n_parsed`, never `n_records` (accolades are idempotent, so
  `n_records` is 0 on a re-run).
- **Parser tests go in `test_mirror.py`, against real mirrored responses**, not
  hand-typed fixtures of what the markup is believed to be.
- **Career awards are not work awards.** The Nobel, SFWA Grand Master and other
  lifetime honours go to `author_accolades`, never `works`.
- ⭐ **`work_key` is Unicode-aware.** Fold accents, then keep `\w` with the
  UNICODE flag; stripping to `[a-z0-9]` collapses every CJK title to "".
- **Scores count distinct award families, never rows.** Hugo, Nebula and Locus
  count as one family, as do the three Bookers. `compute_scores` is a full
  recompute, never incremental.
- **Audio sources rank recordings, not books.** `AUDIO_SOURCES` (Audies,
  Grammy, Listen List, Audible, the charts) and Goodreads' Audiobook category
  never count toward `work_scores`, nor do audio categories inside book sources
  (`AUDIO_JURY_CATEGORIES`, e.g. the LA Times' audiobook production prize); `compute_audio_scores` (`acclaim.py audio`)
  ranks them: audio jury wins and nominations, a top-category bonus, editorial
  lists, chart breadth, plus half the book's score capped at 5. Audio loaders
  don't set `works.form`.
- **Charts are snapshots.** Popularity sources (`source_kind='popularity'`)
  fetch with `_fetch(fresh=True)` or a max page age; everything else reads the
  mirror first. A chart row is one per work, chart and year, keeping the first
  rank seen.
- **Honour crawl delays.** `bayarea_lookup.HOST_SPACING` sets per-host request
  spacing (ala.org and kirkusreviews.com at 10 s). NYT's robots.txt names
  Claude's agents and Publishers Weekly disallows all crawlers — neither is
  scripted.
- **The shelf join has its own strict matcher** (`acclaim.shelf_match`): print
  format, the work's whole stem as the record's title or one of its parts, an
  author among the credits, English; an author-less record needs the exact
  title. Keep it separate from the hot list's loose matcher — each is right for
  its own question.
- **The browser tier can't run on a timer.** `pulitzer.org` 403s scripts
  regardless of headers (TLS fingerprinting), `pen.org` and Douban refuse them
  too, and NYT and WSJ need Darren's login. See `docs/harvesting.md`.
