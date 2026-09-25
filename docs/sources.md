# Source inventory

What `acclaim.py` loads, how, and where it is thin. Coverage is what the
corpus held on 2026-09-15; `uv run acclaim.py stats` gives live counts and the
fetch log.

## Children's books (archive saved 2026-09-18)

`children.py` provides a separate reusable download and offline search, with
3,577 saved records across 16 award sources and 24 reviewed options for ages
2½–3. See [coverage and refresh documentation](children-awards.md), the precise
[source manifest](../data/children/manifest.json), and
[recommendations](../children-books.md). It downloads ALA/ALSC, Ezra Jack Keats,
Carnegie/Greenaway, CCBC Zolotow, Boston Globe–Horn Book, and Bank Street archives,
and reuses children's categories from NBA/Kirkus/Goodreads already in this corpus.
Full world coverage and private nomination ballots are not claimed. This archive
does not automatically add books to the availability polling list.

## Book sources (scriptable, `acclaim.sh` weekly)

| Source | Origin | Loaded | Notes |
|---|---|---|---|
| `hugo` | sfadb.com | 1953–2026 | winners + nominees, every written category incl. short fiction |
| `nebula` | sfadb.com | 1966–2026 | as above |
| `locus` | sfadb.com | 1971–2026 | as above; nominee lists run ~10 deep |
| `booker` | thebookerprizes.com | 1969–2026 | Booker + International (2020–); per-book prize history |
| `nba` | nationalbook.org | 1950–2025 | categories discovered per year |
| `pulitzer`\* | pulitzer.org | 1917–2026 | six book categories |
| `latimes` | latimes.com history page | 1980–2025 | 16 categories |
| `kirkus` | kirkusreviews.com | 2014–2026 | winners and finalists |
| `womens-prize` | womensprize.com (WP REST + book pages) | 1996–2026 | longlist/shortlist/winner |
| `goodreads` | goodreads.com/choiceawards | 2011–2025 | **popular vote**, winner + nominees |
| `nbcc` | bookcritics.org | 2025 | current cycle only |
| `nobel` | api.nobelprize.org | 1901–2025 | **author-level** → `author_accolades` |
| `obama`\* | barackobama.medium.com | 2025–2026 | prefers `harvest/obama.json` |
| `ft-business` | ⚠ Wikipedia | 2005–2025 | FT's shortlists are in varying article prose; rows stamped `via Wikipedia` |

\* Registered with a browser harvest as the preferred input.

sfadb.com (the Locus Index to SF Awards) carries full winner and nominee lists
for all three SF awards on one scriptable site, so it is used over the three
official sites.

## Audiobook sources

These rank recordings and feed only `acclaim.py audio`, never `work_scores`.

| Source | Kind | Origin | Loaded | Notes |
|---|---|---|---|---|
| `audies` | award | audiopub.org | 1996–2026 | winners + finalists, every category; narrators; Audiobook of the Year from 2004 |
| `grammy` | award | ⚠ Wikipedia | 1959–2026 | Best Audio Book / Spoken Word; grammy.com refuses scripts |
| `listen-list` | list | rusaupdate.org + ala.org | 2012–2026 | ALA RUSA outstanding narration, ~12 a year; narrators |
| `audible-best` | list | audible.com | 2022–2026 | editors' Audiobook of the Year + Top 20 (current year) and the genre lists under the best-of-the-year blog tag |
| `audible-charts` | popularity | audible.com/charts | snapshot | top 20 overall + 7 categories; robots.txt forbids paging |
| `librofm` | popularity | libro.fm/bestsellers | snapshot | site-wide top 100 (indie bookstores) |
| `apple-audio` | popularity | Apple marketing RSS | snapshot | US top 100; kids' titles skipped; no narrators |
| `libby-audio` | popularity | OverDrive Thunder API | snapshot | top 300 adult English audiobooks by OverDrive-wide demand |

Goodreads Choice's Audiobook category (2024–) counts as audio popularity too.
Chart sources re-fetch on every run (Audible after 6 days); a chart row records
the first rank seen in a year.

## Browser harvests (`docs/harvesting.md`)

| Source | Why a browser | Harvest | Loaded |
|---|---|---|---|
| `pulitzer` | TLS fingerprinting: 403 to `urllib` and `curl` | `harvest/pulitzer.json` | 1917–2026 |
| `pen` | 403s scripts; archive paginates client-side | `harvest/pen.json` | 1963–2023 |
| `douban` | serves scripts a stub page | `harvest/douban/<year>.json` | 2023–2025 |
| `obama` | Medium 403s scripted clients | `harvest/obama.json` | 2025–2026 |
| `nyt` | subscriber paywall | `harvest/nyt/` — 10 Best 2025, 100 Notable 2025, 100 Best of the 21st Century | 2024–2025 |
| `wsj` | subscriber paywall | `harvest/wsj/` — 10 Best 2025 | 2025 |

## Gaps

- **Pulitzer Memoir/Autobiography** (awarded since 2022). Category ids are 218
  Drama, 219 Fiction, 220 History, 222 Biography, 223 General Nonfiction, 224
  Poetry; 221 is a 404 and Memoir's id is unknown.
- **NBCC** loads the current cycle only; the history pages aren't wired up.
- **Obama** has 2025–2026 only. Older Medium post URLs carry opaque hashes, and
  pre-Medium lists were on Facebook and the White House site.
- **Douban** has 2023–2025. Earlier years use a rank-first layout that needs its
  own parser.
- **Booker Children's** is parsed but has no rows yet.
- **LA Times' "Achievement In Audiobook Production"** is an audio category
  inside a book source; its 4 finalists count toward `work_scores` because
  `is_audio_accolade` recognizes only whole audio sources and Goodreads'
  Audiobook category.
- **`browser-plan` omits Obama.** It is registered as HTTP with a preferred
  harvest, and the plan lists only browser-transport sources.
- **Current-year award pages don't refresh.** Book sources and Audie pages read
  the mirror first, so a page mirrored before its winners were announced keeps
  its old content until its `raw_pages` row is deleted.
- **AudioFile Magazine** best-of lists and Earphones Awards: the archive now
  redirects to kirkusreviews.com, which carries only 2026 monthly and themed
  audiobook lists — no year-by-year archive.
- **Audible** Audiobook of the Year and Top 20 exist for the current year only;
  earlier years survive as genre articles.
- **Audies** 2023 is set in capitals and folded to title case ("January
  Lavoy"); four entries name no author on the page.
- **Not built:** PEN/Faulkner, Baillie Gifford, Dublin Literary, Goldsmiths,
  Carnegie, Edgars, Giller, Miles Franklin, Story Prize, Windham-Campbell,
  Whiting, Costa; NYT bestseller history; literary short fiction (O. Henry,
  Best American Short Stories, Pushcart), whose source would be each
  anthology's contents via ISFDB/MARC 505; ALA Odyssey Award (youth audio).

## Not sources

- **Wikipedia / Wikidata** beyond the two fallbacks above: it has winners but few
  shortlists (21 Pulitzer finalists of ~200, 6 Women's Prize, 1 NBCC as of
  2026-09). Used as a cross-check only.
- **NYT audio bestseller lists**: robots.txt names Claude's agents. A
  subscriber browser harvest or Darren's Books API key would be the route.
- **Publishers Weekly** bestsellers: robots.txt disallows all crawlers.
- **OverDrive `sortBy=popularity` as local demand**: it is platform-wide, used
  only as a chart. `sortBy=mostpopular-site` appears to be library-local and is
  unused.
