# Source inventory

What `acclaim.py` loads, how, and where it is thin. Coverage is what the
corpus held on 2026-09-14; `uv run acclaim.py stats` gives live counts and the
fetch log.

## Scriptable (`acclaim.sh`, weekly)

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
| `audies` | audiopub.org | 1996–2026 | judges the recording; records narrators |
| `nbcc` | bookcritics.org | 2025 | current cycle only |
| `nobel` | api.nobelprize.org | 1901–2025 | **author-level** → `author_accolades` |
| `obama`\* | barackobama.medium.com | 2025–2026 | prefers `harvest/obama.json` |
| `grammy` | ⚠ Wikipedia | 1959–2026 | Best Audio Book; grammy.com refuses scripts |
| `ft-business` | ⚠ Wikipedia | 2005–2025 | FT's shortlists are in varying article prose; rows stamped `via Wikipedia` |

\* Registered with a browser harvest as the preferred input.

sfadb.com (the Locus Index to SF Awards) carries full winner and nominee lists
for all three SF awards on one scriptable site, so it is used over the three
official sites.

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
- **Not built:** PEN/Faulkner, Baillie Gifford, Dublin Literary, Goldsmiths,
  Carnegie, Edgars, Giller, Miles Franklin, Story Prize, Windham-Campbell,
  Whiting, Costa; NYT bestseller history; literary short fiction (O. Henry,
  Best American Short Stories, Pushcart), whose source would be each
  anthology's contents via ISFDB/MARC 505.

## Not sources

- **Wikipedia / Wikidata** beyond the two fallbacks above: it has winners but few
  shortlists (21 Pulitzer finalists of ~200, 6 Women's Prize, 1 NBCC as of
  2026-09). Used as a cross-check only.
- **OverDrive `sortBy=popularity`**: platform-wide demand, not local. Local
  demand is holds per copy in this database.
