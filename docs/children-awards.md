# Children's award lookup

The 2026-09-18 pass saved **3,577 award records across 16 sources** and reviewed
**24 additional English read-aloud options for ages 2½–3**. See
[children-books.md](../children-books.md) for the recommendations and reasons.
The broad archive covers children and teens; it is not a toddler reading list.

## Reuse

```sh
uv run children.py find --age 2.5 --new-only
uv run children.py find --age 3 --new-only --json
uv run children.py find --age 3 --popular
uv run children.py find --age 3 --candidates --new-only
uv run children.py find --award caldecott
uv run children.py find "truck"
uv run children.py pull
uv run children.py pull --refresh
uv run children.py report
```

`find` searches the portable archive without internet access. `--age` selects
human-reviewed suggestions in `data/children/recommendations.json`; its age range
is a read-aloud judgment, not a publisher claim. `--candidates` instead filters
explicit age ranges from CCBC, allowing future archive refreshes to surface
books that have not yet received an individual review. Missing age metadata never
silently passes that filter. `--new-only` compares against titles in the local
catalog and the reader's saved favorites; it does not mean newly published.

The recommendations retain the English-language preference. Spanish, Japanese,
and French availability lookups remain excluded. The award bibliography itself
retains its historical records, including awards to books translated into English.

To start tracking a selected recommendation, copy its `title`, `author`, and
`format` into `wantlist_en.json`. The usual `bayarea_lookup.py` refresh will find
eligible editions. Importing this archive or opening a recommendation creates
no library API requests and does not automatically expand the availability list.

Ten recommendations are in `wantlist_en.json`: *A Ball for Daisy*, *Hot Dog*,
*A Sick Day for Amos McGee*, *Jabari Jumps*, *We All Play*, *Hooray for Hat!*,
*Don't Worry, Little Crab*, *First the Egg*, *Baby Goes to Market* and
*Where's Baby?*.

## Popular acclaim

Every lookup includes popular-acclaim evidence and uses it to order results
within the existing age-fit/support groups, alongside the small personal-fit boost
described below. Age and language filters still apply
first. `--popular` keeps only books with a positive popularity score; `--json`
includes the score, evidence, dates, and source URLs. This is an editorial ranking
aid, not a sales estimate or a probability that a child will enjoy a book.

Each signal family contributes once (duplicate editions/years never multiply it):

- Historical bestseller designation: **2 points**, with publisher attribution;
  it does not imply a current chart position.
- Reader rating at least **4.0/5 from 1,000 ratings**: **2 points**. These are
  explicit editorial thresholds that avoid promoting a tiny enthusiastic sample.
  Lower ratings/counts remain visible but contribute no points.
- Goodreads Choice: **2 points for a win**, **1 for other recorded recognition**,
  retaining the exact stage. Nominations do not claim that readers voted to nominate
  the book. This signal reuses the saved award corpus without new requests.

These points add across families, to a maximum of six. Missing evidence is
**unassessed**, not proof of low popularity. Scores reflect incomplete coverage.
Reader ratings largely reflect adults' opinions, not toddler responses.

[popularity.json](../data/children/popularity.json) records evidence for the
first five selected books: two publisher bestseller claims and three Goodreads
rating snapshots checked on 2026-09-18. Goodreads values came from indexed source pages;
direct page access was blocked, so these are not live counts. Other books may
have Goodreads Choice evidence from the archive but have not had ratings/sales
individually checked. Matching requires both title and author.

To expand or update this reusable lookup, add verified evidence to `popularity.json`
using its existing `bestseller` or `reader-rating` structure, retaining `source_url`,
`checked_at`, and retrieval method. Replace an old rating snapshot with the newer
one instead of accumulating it. Then run `uv run children.py report`.
`pull --refresh` updates award archives; it does **not** refresh these manually
verified popularity snapshots. Find/report remain offline, with zero added
availability API calls.

## Saved favorites

[preferences.json](../data/children/preferences.json) saves the six favorites
reported by the user: *The Watermelon Seed*, *Knuffle Bunny*, *Knuffle Bunny Too*,
*The Very Quiet Cricket*, *The Very Busy Spider*, and *In My Heart*.
These are reading-history feedback, not automatic additions to the want-list.
`--new-only` omits their exact normalized title stems as well as tracked books.

Tentative connections favor feelings/reassurance, family situations, participation,
sound play, and new experiences. A reviewed recommendation with a matching theme
gets **one extra ranking point**, regardless of how many favorites match; its
output names the connected books. Age-fit/support grouping still takes precedence.
This personal-fit point is separate from popular acclaim and cannot qualify a
book for `--popular` by itself. Unreviewed archive entries without thematic
assessments receive no personal boost. Edit this file as tastes develop;
no network requests or database migration are needed.

## Coverage and distinctions

The machine-readable [manifest](../data/children/manifest.json) is the precise
inventory: each archive URL, observed years, statuses, download date, raw filename,
and SHA-256 checksum. The CSV and JSON contain award/category/status observations,
so a book can occupy multiple rows. Raw PDFs and HTML are saved alongside them.

- **Caldecott, Newbery, Geisel, Sibert, Belpré, Batchelder:** complete historical
  bibliographies linked by ALA, through 2026. Winners and honors are separate.
  Belpré retains author/illustrator and young-adult categories.
- **Ezra Jack Keats:** the foundation's full writer/illustrator winner and honor
  archive. Credits are preserved, including source spelling; illustrator awards
  are not silently attributed to the author.
- **Carnegie/Greenaway:** full winner archives and publicly archived shortlists
  from 2010–2026, plus the 2026 longlists. Earlier shortlists are not supplied by
  these archive pages. Before 2007 the winner archive labels publication year;
  `year_basis` preserves this distinction instead of pretending it is ceremony year.
- **Coretta Scott King:** the legacy winner/honor table plus annual pages filling
  missing years through 2026. The historical table does not identify whether a
  record is an author or illustrator award and often omits creator names. These
  fields remain explicitly unknown. Annual pages preserve the categories and
  John Steptoe book awards; lifetime achievement recipients are excluded.
- **Charlotte Zolotow:** the full CCBC archive through 2026, including winners,
  honors, and **highly commended** books as a separate status. The combined
  2021–2022 cycle has `year=2022`, `year_label="2021-2022"`. CCBC's age guidance
  supplies the reusable candidate filter.
- **Boston Globe–Horn Book:** the official history currently spans 1967–2025.
  The 2026 announcement is not yet represented by this historical page.
- **Margaret Wise Brown Board Book Award:** inaugural 2023 winners and the 2025
  gold/silver medalists. The broader recommended board-book list is not mislabeled
  as award nominations. In particular, *Me and the Family Tree* won the 19–36-month
  category, and *We All Play* is a 2025 gold medalist.
- **National Book Award young people's literature, Kirkus young readers, Goodreads
  picture books:** reused from the already downloaded general acclaim corpus.
  Their individual source dates remain in the general SQLite corpus. A child
  archive refresh does not refresh these sources; use `acclaim.py pull --source`
  for the relevant source. Goodreads is a popular vote, not a jury.
- **Irma Black:** the full official history page is saved, but much of it consists
  of cover images without textual titles. It is retained as a raw reference, not
  falsely presented as a complete normalized dataset.

Known extraction defects (3,577 is the export count, not proof of complete
extraction):

- Three titles are truncated where the author's name contains "by": *Hattie Big
  Sky* (Kirby Larson), *Is That the Bus?* (Libby Koponen) and *In Summer Light*
  (Zibby Oneal) are stored as "… by Kir", "… by Lib", "… by Zib" with a
  surname-only author. The credit parser matches "by" inside names.
- Boston Globe–Horn Book 2014 has no rows: the saved page sets those entries
  in `<i>` rather than `<em>`, which the parser skips.

“All notable awards” has no universal registry. This is a substantial US/UK core,
with explicit limits. CBCA, Governor General's, Asian/Pacific American, American
Indian Youth Literature, Schneider, Stonewall, and other national/regional awards
are not comprehensively covered. None of the files claims worldwide completeness.
Private Caldecott/Newbery committee nominations cannot be reconstructed from honors.

## Storage and refresh

`children.py pull` uses the saved source manifest first, then the existing SQLite
raw mirror, then HTTP for missing sources. A fresh clone can rebuild the provided
snapshot without the personal database or any HTTP calls. `--refresh` explicitly
downloads the live official pages; it never substitutes stale bytes on failure.
This snapshot used **38 official archive pages/PDFs**, rather than a request per
book. Existing general-award categories require no new requests.

All sources are parsed and validated before the complete normalized snapshot is
committed to `shelfwalk.db` in `children_award_archive`, then exported as JSON/CSV.
A failed download, missing archive structure, or a per-source count loss greater
than 10% leaves the previous parsed snapshot and exports intact. New downloaded
files use content hashes so a failed refresh cannot overwrite the bytes referenced
by the previous manifest. Historical files remain available for parser debugging.

`sources/children_awards.py` contains the parsers; `test_mirror.py` checks the
actual saved publisher/award-body bytes. `test_children.py` verifies offline
rebuilding, age filters, title/author identity, and refresh failure preservation.
All markdown generation is in `report.py`.

## Selection notes

Award recognition starts the search. The toddler shortlist favors short repeated
text, clear picture sequences, actions/sounds, familiar situations, and opportunities
to point or predict. Longer plots and mild suspense are flagged separately. It
includes a few additional publisher-verified options outside the award corpus,
such as *Don't Worry, Little Crab* and *Red Sled*.

The imported ALA bibliography identifies *Where's Baby?* as a **2021 Geisel Honor**.
Its publisher page currently says 2020; the award body's record takes precedence.
For *Every Monday Mabel*, CCBC's age guidance starts at two while the publisher
says four to eight. Both are disclosed rather than silently replacing one.
