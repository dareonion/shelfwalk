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
