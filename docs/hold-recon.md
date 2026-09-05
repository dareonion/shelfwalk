# Placing holds: what still has to be captured

`hotlist.py` watches and decides. It does not yet place. `BiblioCommonsHolds`
and `WebPacHolds` raise `NotImplementedError` on purpose — the endpoints they
need have not been observed, and guessing at an authenticated write endpoint is
how you end up with twenty accidental holds instead of one.

Everything else works without any of this: watching, the sighting history, the
holds-per-copy ranking and the dry run all use the same unauthenticated public
catalog reads the rest of the repo uses.

## Before anything: is this worth doing?

Two honest caveats, neither of them a reason not to, but both worth deciding
once rather than discovering later.

- **Library terms of use.** Automating account actions is very likely outside
  the letter of SCCL's and San José's terms, which contemplate a person at a
  browser. This is one household, a handful of titles, a few requests a season,
  on the account holder's own card — but it is not *nothing*, and it is worth a
  deliberate decision rather than a drifted-into one.
- **A hold is not free to anyone else.** Every speculative hold takes a copy
  out of circulation for the next patron for the length of the pickup window.
  That is the real argument for `max_holds_per_copy`, for one hold per title
  rather than one per system, and for cancelling promptly when a title arrives
  from somewhere else first.

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
`request` path off the record URL. The field names differ between installs and
have changed across releases, so they must be read off the real form rather
than copied from another library's.

## Storing the credentials

Keychain only. Nothing in the repo, nothing in the environment, nothing in a
log. `hotlist.secret()` reads these at the moment of use:

```sh
secret-tool store --label='shelfwalk sjpl barcode' \
    service shelfwalk system sjpl field barcode
secret-tool store --label='shelfwalk sjpl pin' \
    service shelfwalk system sjpl field pin
```

`hotlist.py holds` skips any system with no stored credential, so the watcher
keeps working for systems you have not set up (and for a card that has
expired — which is what happened to the San José card that prompted all this).

## Turning it on

1. Store credentials for one system only.
2. `uv run hotlist.py holds` — read the dry run and agree with it.
3. `uv run hotlist.py holds --place` by hand, once, and confirm in the catalog.
4. Only then uncomment `SHELFWALK_PLACE_HOLDS=1` in
   `systemd/shelfwalk-hotwatch.service`.

The batch cap (`--limit`, default 5) refuses the *whole* run rather than
placing the first five: a matcher bug that suddenly plans twenty holds should
place none of them, not five arbitrary ones.
