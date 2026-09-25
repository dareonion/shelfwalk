# shelfwalk

Three tools on one SQLite store (`shelfwalk.db`):

- **Want-list** — which Bay Area branches have a toddler's want-list on the
  shelf right now, across every edition (board/picture printings, audiobooks,
  eBooks, Chinese translations), with every
  listing linked to its catalog record.
- **Hot list** — a few adult new releases, watched for the shortest hold queue.
- **Acclaim** — award and best-of lists, joined to what's borrowable.

## Setup

```bash
uv sync    # pypinyin; playwright only for the retired Peoria scraper
```

The Bay Area catalogs are plain HTTP — no browser or credentials.

## Files

| File | Role |
|---|---|
| `bayarea_lookup.py` | want-list lookups, matching, availability, enrichment |
| `report.py` | renders every markdown report from the store |
| `catalog_db.py` | schema and shared queries |
| `hotlist.py`, `hotlist.json` | hot-release watcher and its watchlist |
| `acclaim.py`, `acclaim_core.py`, `sources/` | awards corpus: CLI, shared machinery, one adapter per awarding body |
| `children.py`, `sources/children_awards.py`, `data/children/` | saved children's awards, age-reviewed recommendations, offline search |
| `tools/collector.py` | localhost sink for browser-side harvests |
| `refresh.sh`, `hotwatch.sh`, `acclaim.sh`, `systemd/` | scheduled jobs |
| `library_lookup.py`, `ingest.py` | retired Peoria scraper and its JSON loader |
| `test_*.py` | tests (`test_mirror.py` runs against the local mirror) |
| `docs/` | source inventory, browser harvesting, hold placement, children's awards, availability research |

**Generated reports** — never hand-edit; `uv run report.py --write`:

- `bayarea.md` — **start here**: the to-do ladder, your branches, title × system
- `sccl.md` / `sjpl.md` / `mountainview.md` — per-system shelf lists by branch
- `mountainview-linkplus.md` — Mountain View-owned LINK+ holdings, with source dates
- `linkplus.md` — LINK+ union catalog, per title
- `titles.md` — ages, ISBN, awards and summary per title
- `children-books.md` — more books for ages 2½–3, with award evidence and source coverage
- `books.md`, `north.md`, `lakeview.md`, `main.md` — frozen Peoria snapshot

## Finding more children's books

Start with [the reviewed shortlist](children-books.md). The saved archive contains
3,586 award records across 16 sources, with original PDFs/HTML, a portable
[CSV](data/children/awards.csv), [JSON](data/children/awards.json), and a
[coverage manifest](data/children/manifest.json). It covers major US/UK awards,
not every award worldwide. Public honors and shortlists are distinguished from
nominees; private committee nominations are not available.

```sh
uv run children.py find --age 2.5 --new-only
uv run children.py find --age 3 --new-only
uv run children.py find --age 3 --popular
uv run children.py find --age 3 --candidates --new-only
uv run children.py find --award geisel
uv run children.py find "truck"
uv run children.py pull             # reuse saved sources, even on a fresh clone
uv run children.py pull --refresh   # download current official archives
uv run children.py report           # regenerate children-books.md
```

`--age` selects the 24 individually reviewed read-aloud suggestions in
`data/children/recommendations.json`; `--candidates` explores other books with
explicit source age guidance. Searching and reporting make **zero network
calls**, and importing awards adds nothing to library polling. Within each
age-fit group, results factor in saved popularity evidence and tentative theme
connections to the child's saved favorites (`data/children/preferences.json`);
`--popular` requires positive evidence, and missing coverage is unknown, not
unpopular. `--new-only` omits tracked titles and saved favorites. Ten of the
reviewed recommendations are on the English want-list. See
[docs/children-awards.md](docs/children-awards.md) for coverage, scoring,
refresh behavior and how to add books to the want-list.

## Want-list

| Key | System | Catalog |
|---|---|---|
| `sccl` | Santa Clara County Library District | BiblioCommons (gateway JSON API) |
| `sjpl` | San José Public Library | BiblioCommons (gateway JSON API) |
| `mvpl` | Mountain View Public Library | classic Innovative WebPAC — **skipped** |
| `linkplus` | LINK+ union catalog (~70 CA/NV systems) | INN-Reach WebPAC |

**Mountain View is skipped unless named** (`--system mvpl`): its catalog refuses
searches not launched from its own search page (since 2026-09-12) and its
robots.txt disallows crawlers. Its report lists holdings with their
last-checked date and no shelf claims; record links still load, so check a
title there by hand.

**Mountain View via LINK+** appears separately in the overview, favourite
branches and `mountainview-linkplus.md`. It uses Mountain View-owned holdings
already collected by the LINK+ lookup, so it adds no requests. Coverage is
partial: a missing title means unknown. Links retain their LINK+ record IDs,
and each source observation shows its shelf location, call number and date.
Fresh available copies count toward the local shelf/hold ladder.

Shelf observations expire after **20 hours**, including when every system
stops refreshing or only some titles succeed. Older observations remain in
the database but show unknown status in reports. A title refresh publishes
all its editions together; an empty successful result replaces old copies,
while a failed edition leaves the previous complete snapshot intact.

A LINK+ hit can be requested for pickup at a member library. Systems run in
parallel, one thread each; each host gets serial, spaced requests (LINK+ at
1s).

```bash
uv run bayarea_lookup.py                          # every title, default systems
uv run bayarea_lookup.py --system sccl --limit 5  # spot check
uv run bayarea_lookup.py --resume                 # only titles not yet looked up
uv run bayarea_lookup.py --retry-misses           # also redo titles that never matched
uv run bayarea_lookup.py --rediscover             # force searches for all selected titles
uv run bayarea_lookup.py --title "dear zoo"       # ad-hoc probe; prints, stores no results (pages still mirrored)
uv run bayarea_lookup.py --enrich                 # just the record-detail pass
```

**Matching.** Each title is searched as cleaned title + author surname and
candidates are scored on normalized title similarity; "not held" is recorded
too. CJK titles are searched in CJK where the catalog supports it and as pinyin
where it doesn't, and pinyin-vs-pinyin only counts when nearly exact.

**Versions.** Every version of a matched work is tracked (`remote_editions`):
other printings, audiobooks (including compilations that carry the story under
another title), eBooks and translations. Translations and compilations are
accepted only from strict AND-semantics searches with a matching author.
Extra translations are limited to Chinese. Records identified as
Spanish, Japanese or French are excluded from matching, cached availability polling
and enrichment, including audio/digital editions.
Digital editions are linked but carry no shelf state. An enrichment pass after
each lookup reads every version's record page (MARC 505/240) for contents,
stated original title and full bibliographic detail
(`remote_editions.details`).

**Want-list files.** `wantlist_{en,zh,fr}.json` are arrays of
`{title, author, format, lang?, isbn?}`; `lang` pins a title to that language,
and `isbn` gives an unmatched title one last search. `wantlist_exclude.json`
lists titles to leave out of remote lookups. Adding a book is one line plus
`uv run bayarea_lookup.py --resume`.

**The to-do ladder** in `bayarea.md` puts each title in one rung: on a
favourite branch's shelf (nothing to do) → place a hold (with whether a copy
sits on another branch's shelf) → request through LINK+ → buy.

**Raw mirror.** Every response is stored verbatim in `raw_pages` (newest per
URL), so a parser or matching fix replays offline instead of re-scraping.

**Request volume.** Routine SCCL, SJPL and LINK+ runs reuse discovered editions
and misses for seven days. They fetch availability once per distinct physical
bib per run, sharing that result across wanted titles. They keep enrichment
and the original discovery date; daily polling does not extend the search
cache. New entries and changes to title, author, format, ISBNs or language pin
trigger searches immediately. A failed cached bib keeps its previous complete
snapshot and triggers rediscovery on the next run.

An offline replay of all 165 titles on 2026-09-18 measured:

| System | Before | Routine refresh |
|---|---:|---:|
| SCCL (JSON API) | 606 | 210 |
| SJPL (JSON API) | 616 | 210 |
| LINK+ (HTML requests) | 555 | 355 |
| Total | 1,777 | 775 |

The replay ran entirely from `raw_pages` with no missing responses or errors,
and counts repeated URLs and editions later rejected by enrichment (SCCL before:
216 smart searches, 165 fielded searches, 225 availability calls). That is a 56%
reduction. Counts exclude retries and uncached detail pages. Expired mappings and new or
changed titles add discovery requests. Newly acquired titles/editions may take
up to seven days to appear; `--rediscover` forces discovery now. `--retry-misses`
forces searches for previously missed titles, while `--resume` skips titles
already searched and is not an availability refresh. Hot-list discovery stays
frequent because new preorder listings and hold queues are time-sensitive.

## Hot list

A new release has no copy on a shelf; what decides when you read it is your
place in the hold queue, fixed weeks before publication.

```bash
uv run hotlist.py add "Taipei Story" --author Kuang --isbn 9780063473744
uv run hotlist.py check      # poll every watched title
uv run hotlist.py status     # standing per title, shortest joinable queue
uv run hotlist.py holds      # what auto-hold would do (dry run)
```

```
Taipei Story  (pub 2026-09-08)
  sjpl  BK           copies   28  holds    1   0.04/copy  avail   0 (no holds — walk-in only)
  sjpl  BK           copies   50  holds   18   0.36/copy  avail   0
  sccl  BK           copies   38  holds   60   1.58/copy  avail   0
  → shortest joinable queue: sjpl (0.36 holds/copy)
```

Watching needs no card. Placing holds needs a credential in the login keyring
*and* `SHELFWALK_PLACE_HOLDS=1` in the systemd unit, and the placers are not
implemented until their endpoints are captured — see `docs/hold-recon.md`.

## Acclaim

```bash
uv run acclaim.py pull --all        # every source that runs unattended
uv run acclaim.py browser-plan      # what needs a Chrome pass
uv run acclaim.py stats             # coverage and provenance
uv run acclaim.py score             # rank books by breadth across independent juries
uv run acclaim.py audio             # rank audiobooks: audio juries, lists, charts
uv run acclaim.py shelf             # top-scored works with a copy available now
uv run acclaim.py find "<story>"    # which book carries a short work
```

Sources are the awarding bodies themselves; Wikipedia is only a fallback, since
it lists winners but few shortlists. Audiobook sources — the Audies, the
Grammy, the ALA Listen List, Audible's Best of the Year, and bestseller charts
from Audible, Libro.fm, Apple Books and OverDrive — feed a separate audiobook
ranking and never a book's own score. Short fiction is tracked too, with
`work_containers` recording which collection or anthology reprints it.
`docs/sources.md` is the inventory; sites that refuse scripts are harvested in
a real Chrome tab (`docs/harvesting.md`).

## Scheduled jobs

| Timer | Runs | When |
|---|---|---|
| `shelfwalk-refresh` | `refresh.sh`: lookups, reports, commit of the generated reports (`SHELFWALK_PUSH=1` pushes) | daily 07:30 |
| `shelfwalk-hotwatch` | `hotwatch.sh`: hot-list check, desktop notice when something moves | every 4 h |
| `shelfwalk-acclaim` | `acclaim.sh`: `acclaim.py pull --all` | Sunday 04:00 |

```bash
cp systemd/shelfwalk-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shelfwalk-refresh.timer   # likewise hotwatch, acclaim
journalctl --user -u shelfwalk-refresh -n 20
```

Logs go to `logs/`. `loginctl enable-linger` keeps timers running while logged
out.

## Tests

```bash
uv run pytest -q    # no network; test_mirror.py skips pages not in the local mirror
```

## Peoria (retired)

`library_lookup.py` drove a real browser past the Peoria Public Library
catalog's Cloudflare check; `ingest.py` loads its JSON. Neither is refreshed,
and the Peoria reports are a frozen snapshot.

```bash
uv run playwright install chromium                       # one-time
uv run library_lookup.py --details "little blue truck"   # per-branch holdings
uv run library_lookup.py --connect http://127.0.0.1:9222 --details "dear zoo"
```
