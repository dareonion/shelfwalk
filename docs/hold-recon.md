# Placing holds: what still has to be captured

`hotlist.py` watches and decides. It does not yet place. `BiblioCommonsHolds`
and `WebPacHolds` raise `NotImplementedError` on purpose — the endpoints they
need have not been observed, and guessing at an authenticated write endpoint is
how you end up with twenty accidental holds instead of one.

Everything else works without any of this: watching, the sighting history, the
holds-per-copy ranking and the dry run all use the same unauthenticated public
catalog reads the rest of the repo uses.

## Decide first

- **Library terms of use.** Automating account actions is likely outside the
  letter of SCCL's and San José's terms, even for one household's own card and
  a few holds a season.
- **A hold isn't free to anyone else.** A speculative hold keeps a copy from the
  next patron through the pickup window — hence `max_holds_per_copy`, one hold
  per title rather than per system, and cancelling as soon as a copy arrives
  from elsewhere.

## What to capture

With a logged-in session in your own browser, open devtools → Network, place
one real hold by hand, and record:

### BiblioCommons (SCCL, San José)

The gateway API this repo already uses (`gateway.bibliocommons.com/v2/...`) is
read-only and anonymous. Holds go somewhere else. Capture:

1. **The login POST** — URL, form fields, and what comes back (a session
   cookie, and probably a CSRF token in a `<meta>` tag or a `set-cookie`).
2. **The hold POST** — URL, JSON body (bib id, pickup branch id, patron id),
   and every header that is not obviously boilerplate (`x-csrf-token`,
   `x-requested-with`).
3. **The pickup-branch identifiers.** They are opaque ids, not the branch names
   in `hotlist.json`; the mapping needs recording.
4. **The response on success and on the two failures worth handling** — already
   held, and hold limit reached.

### Innovative WebPAC (Mountain View)

Classic Innovative authenticates with name + barcode + PIN and posts to a
`request` path off the record URL; field names vary by install, so read them off
the real form. Mountain View is out of the default watch
(`bayarea_lookup.SKIPPED_SYSTEMS`), so this placer only matters for a watchlist
entry that names `mvpl`.

## Storing the credentials

Keychain only. Nothing in the repo, nothing in the environment, nothing in a
log. `hotlist.secret()` reads these at the moment of use:

```sh
secret-tool store --label='shelfwalk sjpl barcode' \
    service shelfwalk system sjpl field barcode
secret-tool store --label='shelfwalk sjpl pin' \
    service shelfwalk system sjpl field pin
```

`hotlist.py holds` skips any system with no stored credential, so watching
keeps working for systems not set up and for an expired card.

## Turning it on

1. Store credentials for one system only.
2. `uv run hotlist.py holds` — read the dry run and agree with it.
3. `uv run hotlist.py holds --place` by hand, once, and confirm in the catalog.
4. Only then uncomment `SHELFWALK_PLACE_HOLDS=1` in
   `systemd/shelfwalk-hotwatch.service`.

The batch cap (`--limit`, default 5) refuses the *whole* run rather than
placing the first five: a matcher bug that suddenly plans twenty holds should
place none of them, not five arbitrary ones.
