# Source inventory

What `acclaim.py` pulls from, how, and where each one is thin. Coverage gaps
are recorded rather than smoothed over: a source that returns little should be
visibly a thin *fetch*, not silently a thin field.

Run `uv run acclaim.py stats` for live counts and the provenance log.

## Working, scriptable (run unattended by `acclaim.sh`)

| Source | Origin | Coverage | Notes |
|---|---|---|---|
| `hugo` | sfadb | 1953– | winners + nominees, all written categories incl. short fiction |
| `nebula` | sfadb | 1966– | as above |
| `locus` | sfadb | 1971– | as above; nominee lists run ~10 deep per category |
| `booker` / `booker-intl` / `booker-childrens` | thebookerprizes.com | 1969– | per-book prize history; longlist/shortlist/winner |
| `nba` | nationalbook.org | 1950– | categories discovered per year from the nav |
| `obama` | barackobama.medium.com | recent | see gap below |
| `nobel` | api.nobelprize.org | 1901– | **author-level** → `author_accolades` |
| `nbcc` | bookcritics.org | current cycle | see gap below |
| `goodreads` | goodreads.com/choiceawards | 2009– | **popular vote, not a jury** — winner + 20 nominees per category |
| `latimes` | latimes.com/events/festival-of-books/book-prizes | current cycle | 15 categories; winner ribbon is a separate module |
| `ft-business` | ⚠ Wikipedia (FT paywalled prose) | 2005–2025 | verified against ft.com; every row stamped `via Wikipedia` |

`sfadb.com` is the Locus Index to SF Awards. One scriptable site carries
complete winner *and* nominee lists for all three SF awards, every category,
which is why it is preferred over three separate official sites that are each
patchier.

## Needs a Chrome pass (see `harvesting.md`)

| Source | Why | State |
|---|---|---|
| `pulitzer` | TLS fingerprinting — 403s `urllib` *and* `curl` | harvested: 1,116 rows, 1917–2026 |
| `pen` | 403s scripts; archive paginates client-side | harvested: 999 rows, 1963–2023 |
| `douban` | serves scripts a 2.4KB stub | harvested: 540 books, 2023–2025 |
| `obama` | Medium 403s scripted clients persistently | harvested: 2 posts, 2025–2026 |
| `nyt` | paywall (subscriber) | harvested: 100 Best of the 21st C. |
| `wsj` | paywall (subscriber) | harvested: 10 Best of 2025 |
| NYT | paywall (10 Best, 100 Notable, 100 Best of the 21st C., bestsellers) | not built |
| WSJ | paywall (Best Books of the Year) | not built |

Three different walls, three different workarounds, all in `harvesting.md`:
Pulitzer POSTs to the localhost collector; PEN and Douban cannot (CSP and
Private Network Access respectively) and use a blob download instead — which
needs downloads allowed for the site.

## Known gaps, honestly

- **Pulitzer Memoir/Autobiography** (added 2022) is missing. Category ids are
  sequential — 218 Drama, 219 Fiction, 220 History, 222 Biography, 223 General
  Nonfiction, 224 Poetry — and 221 is a 404. Where Memoir lives is unfound.
- **NBCC is current-cycle only.** The landing page carries just this year;
  historical years are on separate pages not yet wired up.
- **Obama's backfill is shallow, and needs a browser.** Medium 403s scripted
  clients persistently, so `load_obama` prefers `harvest/obama.json` from a
  Chrome pass. Only 2025 and 2026 are captured; older post URLs carry an opaque
  hash, and pre-Medium lists were on Facebook and the White House site.
  ⚠ The two post shapes divide their sections differently — the year-end post
  says "Favorite Movies of \<year\>", the summer post says "Summer Playlist:".
  Missing the second put 46 songs into the corpus as books.
- **Douban only covers 2023–2025.** Those years share one layout (rating,
  title, credit). 2022 and earlier use a rank-first layout where the rating is
  sometimes glued to the title line and sometimes on its own — ambiguous
  enough to need its own parser rather than a guess.
- **LA Times and NBCC are current-cycle only.** Both have history pages that
  are not yet wired up. The LA Times Innovator's and Kirsch awards are career
  honours with no book, so they correctly produce no work-level rows.
- **Double-encoded UTF-8 is real and rare.** PEN serves 5 of 999 rows with a
  curly apostrophe mangled into three Latin-1 characters — exactly the
  frequency that survives a spot-check and then quietly poisons a `work_key`.
  `demojibake()` repairs it at ingest for every browser-harvest loader.
- **Women's Prize is not built.** `womensprize.com/books/` renders its listing
  client-side and the `_prize_year` filter is not in the HTML, so it needs more
  recon than a class-name scrape.
- **Not yet built:** PEN America/Faulkner, Kirkus, Baillie Gifford, Dublin
  Literary, Goldsmiths, ALA Carnegie, Edgars, Giller, Miles Franklin, Story
  Prize, Windham-Campbell, Whiting, Costa (defunct 2022). Each is one parser
  plus one `Source(...)` line.
- **Literary short fiction** (O. Henry, Best American Short Stories, Pushcart)
  is not built. These have no official index — they *are* annual anthologies,
  so the anthology's table of contents is the source, via the same
  ISFDB/MARC-505 path as the SF short fiction.

## Deliberately not a source

- **Wikipedia / Wikidata**, except as a fallback and cross-check. Measured
  2026-09-06: 818 Booker nominees but 21 Pulitzer finalists of ~200, 6 Women's
  Prize, 1 NBCC. Good for winners, useless for shortlists.
- **OverDrive `sortBy=popularity`** for the popularity ranking: it is
  platform-wide demand, not local. Local demand comes from holds-per-copy in
  this same database, which is real Bay Area signal.

## Lessons that cost real time

- ⭐ **`work_key` must be Unicode-aware.** The first version stripped every
  character outside `[a-z0-9]`, so **every CJK title folded to the empty
  string** and all 540 Douban books collapsed into 26 keys. It was caught only
  because `stats` showed 39 accolades for 540 harvested books. It now folds
  accents (NFKD, drop combining marks) *then* keeps Unicode word characters,
  so `château`/`chateau` still meet and 九诗心 survives. A genuinely
  punctuation-only title — Fady Joudah's `[...]` — still folds to empty and is
  carried by its author, which is correct.
- **Fixed line offsets are guesses about layout.** The NYT list broke twice:
  six titles wrap onto two lines, and one carries its author inside the title
  so its byline line is a bare year.
- **Wiki markup is not consistent within one page.** The FT list used
  `{{Blue ribbon}}` in some years and `{{blue ribbon}}` in others (14 of 21
  winners lost to a case-sensitive check), laid 2020 out as a table rather than
  a list (whole year lost), and needed a section regex that stops at *any*
  heading level rather than just `===`.
- **Every one of those failed silently.** None raised; each just returned less.
  That is why `acclaim_fetches` logs counts and `stats` is worth reading
  against what you think you harvested.
