# The browser tier: harvesting sources that block scripts

Most award sites yield to plain HTTP and are handled by `acclaim.py pull`
without anyone watching. A few do not, and this is the recipe for those.

## Why a browser at all

Two different walls, same workaround:

- **Bot fingerprinting.** `pulitzer.org` returns 403 to `urllib` *and* to
  `curl`, with browser-identical headers, cookies and referer. That is TLS
  fingerprinting: the handshake gives away the client before a header is read,
  so no amount of header spoofing helps. The page itself is plain
  server-rendered HTML and complete back to 1917 — nothing is missing, it just
  will not hand it to a script.
- **Paywalls.** NYT and WSJ need Darren's own logged-in session.

In both cases `fetch()` running *inside* a real Chrome tab is the browser, so
it passes. The page does the fetching and parsing; the result is POSTed to a
localhost sink so a 250KB harvest never travels through a conversation.

## The mechanism

```
tools/collector.py            # localhost:8765, writes harvest/<name>.json
   ↑ POST (from page JS)
Chrome tab on the target site
```

Start the sink, then run the page-side script:

```sh
uv run --no-project python tools/collector.py     # leave it running
```

```js
// in the target tab, via the browser's console or claude-in-chrome
const rows = /* …scrape the DOM… */;
await fetch('http://127.0.0.1:8765/collect?name=pulitzer',
            {method:'POST', headers:{'Content-Type':'text/plain'},
             body: JSON.stringify(rows)});
```

Then parse offline: `uv run acclaim.py pull --source pulitzer --include-browser`

### Two traps, both of which cost time here

1. **Chrome's Private Network Access silently drops the POST.** A public HTTPS
   origin reaching `127.0.0.1` gets a preflight even for an otherwise-simple
   POST, and it is refused unless the response carries
   `Access-Control-Allow-Private-Network: true`. Without it the request never
   arrives and the collector just looks idle — there is no error anywhere.
   `tools/collector.py` sends it.
2. **Sequential fetches blow the 45s CDP timeout.** Six category pages fetched
   one after another timed out; `Promise.all` over the same six finished well
   inside the limit. Parallelise the fetches, then parse.

## Per-source recipes

### Pulitzer (`harvest/pulitzer.json`)

Category ids are sequential Drupal term ids, discovered from the prev/next
links: **218 Drama, 219 Fiction, 220 History, 222 Biography, 223 General
Nonfiction, 224 Poetry.** (221 is a 404; the Memoir/Autobiography category,
added 2022, has not been located yet and is the one known gap.)

Structure is one `div.accordion-item` per year, with the year in an
`a[href*="prize-winners-by-year/"]`, and each entry an anchor whose href says
which it is: `/winners/` or `/finalists/`. The winner's citation is the
sibling `.winner-citation`.

Yield: 1,116 rows, 1917–2026.

```js
const CATS={218:'Drama',219:'Fiction',220:'History',222:'Biography',
            223:'General Nonfiction',224:'Poetry'};
const pages=await Promise.all(Object.keys(CATS).map(id=>
  fetch(`/prize-winners-by-category/${id}`,{credentials:'same-origin'})
    .then(r=>r.text()).then(t=>[id,t])));
const rows=[];
for (const [id,html] of pages){
  const doc=new DOMParser().parseFromString(html,'text/html');
  for (const item of doc.querySelectorAll('div.accordion-item')){
    const ya=item.querySelector('a[href*="prize-winners-by-year/"]');
    const year=ya?parseInt(ya.textContent.trim(),10):null;
    for (const a of item.querySelectorAll('a[href*="/winners/"],a[href*="/finalists/"]')){
      const won=/\/winners\//.test(a.getAttribute('href'));
      rows.push({category:CATS[id],year,status:won?'winner':'finalist',
        raw:a.textContent.trim().replace(/\s+/g,' '),person_url:a.href,
        citation:won?((item.querySelector('.winner-citation')||{}).textContent||'')
          .trim().replace(/\s+/g,' '):''});
    }
  }
}
await fetch('http://127.0.0.1:8765/collect?name=pulitzer',
  {method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify(rows)});
rows.length
```

### NYT and WSJ

Not yet written. These need Darren's session, and scraping them under his
login is a personal-use decision worth making deliberately rather than by
default — the same call as the auto-hold question in `docs/hold-recon.md`.

Targets when they are built: NYT 10 Best Books, 100 Notable Books, the 100
Best Books of the 21st Century, and the weekly bestseller history (the only
real popularity source available); WSJ Best Books of the Year.
