# Library availability research

Researched 2026-09-18. Mountain View's usable fallback is already in the
database: LINK+ holdings with `branch = 'Mountain View Public'`, now exposed as
the report-only `mountainview-linkplus.md` view (no new catalog client, no added
requests). For supported batch access to the local catalog, Sierra's
authenticated API is the strongest option, subject to the library issuing
credentials. Request volume for the existing systems is in the README.

## Mountain View via LINK+

From `catalog_db.latest_remote_availability(conn, 'linkplus')`, filtered to
currently matched records owned by Mountain View:

| Measurement | Result |
|---|---:|
| Snapshot checked_at | 2026-09-15T07:31:52 |
| Want-list titles represented | 59 |
| Holdings rows | 166 |
| Rows marked available | 36 |
| Distinct titles with an available row | 22 |

These are historical observations, not a fresh scan of all 59 titles. Counts
are holdings rows, not a verified count of unique physical items; the schema
does not store item IDs and the same bib can match more than one wanted title.

One live HTTP request on 2026-09-18 to the existing
[LINK+ record for The Watermelon Seed](https://csul.iii.com/search?/.b51876403/.b51876403/1,1,1,B/detlframeset~b51876403&FF=&1,0,)
returned:

```text
Mountain View Public
Children's Board Books - 1st Floor
J BOARD P
AVAILABLE
```

The HTML identifies the owning library with `class="holdings9mvpl"`, and
`linkplus_parse_holdings()` already parses library, shelf, call number and
status; no login, browser session or new parser is needed. The vendor's
[LINK+ membership directory](https://knowledge.ag-software.clarivate.com/linkplus/members/public_library_members.htm)
confirms Mountain View's `9mvpl` code and Sierra ILS.

The view keeps the LINK+ bib ID, record link, source and observation time, and
never overwrites direct `mvpl` records with union-catalog IDs. An absent row is
unknown: the lookup searches selected editions, caps LINK+ matches at four per
wanted title, and does not establish full local-catalog coverage. The live
result verifies access, not the upstream status synchronization delay or
guaranteed walk-in availability.

## Mountain View's own catalogs

- A live read of the [classic catalog robots.txt](https://classiccatalog.mountainview.gov/robots.txt)
  on 2026-09-18 still returned `User-agent: *` and `Disallow: /`; scripted
  searches stay skipped. Blocked searches were not repeated and headers were
  not changed to evade the restriction.
- Search indexes contain an official [Aspen catalog help page](https://catalog.mountainview.gov/cataloghelp),
  but `catalog.mountainview.gov/robots.txt` failed with a TLS handshake error
  from this environment and the help page could not be fetched live. This is an
  unverified lead, not evidence of a usable Aspen API. The repo elsewhere refers
  to Vega; neither description alone establishes which discovery frontend is
  operational now.
- The library's [digital collections page](https://library.mountainview.gov/borrow/ebooks-eaudiobooks)
  names Northern California Digital Library as its OverDrive collection, a
  separate digital-availability opportunity.

## Bulk API options

| Service | What it can provide | Access and fit |
|---|---|---|
| Sierra REST API | Item records for multiple bib IDs, including location, call number and circulation status | Library-issued API key/secret with read roles. Relevant to Mountain View. |
| OverDrive Availability v2 | Availability, copies and holds for up to 50 titles per request within one collection | Approved API credentials and OAuth. Digital books/audio only. |
| Existing BiblioCommons gateway | Search responses contain multiple bibs and aggregate availability; the repo fetches branch/item availability per bib | Already used for SCCL/SJPL. No supported multi-bib branch-availability endpoint was verified. |
| WorldCat Search API | Bibliographic discovery and which institutions hold a title | Subscription/partner access; does not by itself establish local shelf status. |
| WMS Availability API | Live circulation availability for WMS libraries | WMS library access required. Not a direct replacement for Mountain View's Sierra system. |

Sierra's [query parameter documentation](https://knowledge.ag-software.clarivate.com/sierra/Sierra_API/zAPIs/queryParameters.htm)
supports comma-separated `bibIds` on the items endpoint. Illustrative request,
with the host and IDs supplied by the library:

```http
GET /iii/sierra-api/v6/items?bibIds=1234567,2345678&fields=id,bibIds,location,status,callNumber
Authorization: Bearer <access-token>
```

Results are paginated: an ID batch does not guarantee that every associated
item fits in one response. Convert WebPAC IDs carefully; this parameter expects
the numeric record ID without the record type prefix or check digit. Ask the
library to confirm that mapping before importing the existing cached IDs.

The [item schema](https://techdocs.iii.com/sierraapi/Content/zObjects/itemObjectDescription.htm)
contains the needed fields. Interpret status and due date together and exclude
deleted/suppressed records; a status code alone is not a sufficient shelf test.
Library administrators assign [API read roles](https://documentation.iii.com/sierrahelp/Content/sadmin/sadmin_other_webapps_api.html).

OverDrive documents a [50-title bulk request](https://developer.overdrive.com/api-docs/discovery-apis/library-availability):

```http
GET https://api.overdrive.com/v2/collections/<collection-id>/availability?products=<title-id>,<title-id>
Authorization: Bearer <access-token>
```

The identifiers are OverDrive title IDs or reserve IDs, not arbitrary ISBNs;
resolve them first, and do not mix identifier types in a batch. This suits a
future Libby queue watcher, not physical shelf lists.

[WorldCat Search](https://www.oclc.org/developer/api/oclc-apis/worldcat-search-api.en.html)
and [WMS Availability](https://www.oclc.org/developer/api/oclc-apis/wms-availability-api.en.html)
have different purposes and access requirements. Open Library's
[APIs and data dumps](https://openlibrary.org/developers/api) help with book
metadata but not Mountain View's circulation state. No unrestricted
cross-library bulk shelf-status API was verified.

## Open

- **Sierra read access for Mountain View.** The request to the library would be
  Bibs Read and Items Read access, the API base URL, approved polling limits
  and local status/location mappings; a scheduled item-status export is the
  alternative if API access is unavailable (no public export was verified).
  No contact was sent (as of 2026-09-18).
