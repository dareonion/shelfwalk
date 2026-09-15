# The browser tier: sources that refuse scripts

Most award sites work over plain HTTP and load unattended with
`acclaim.py pull`. A few don't: `pulitzer.org` 403s `urllib` and `curl` even
with browser-identical headers (TLS fingerprinting — the handshake gives the
client away), `pen.org`, Medium and Douban refuse scripted clients, and NYT and
WSJ need Darren's subscription. JavaScript running inside a real Chrome tab
passes all of them, so the page does the fetching and parsing, and only the
result leaves the browser. `uv run acclaim.py browser-plan` lists what is
harvested.

## Mechanism

```sh
uv run --no-project python tools/collector.py   # localhost:8765 → harvest/<name>.json
```

```js
// in the target tab (devtools console or claude-in-chrome)
const rows = /* …scrape the DOM… */;
await fetch('http://127.0.0.1:8765/collect?name=pulitzer',
            {method: 'POST', headers: {'Content-Type': 'text/plain'},
             body: JSON.stringify(rows)});
```

Then parse offline: `uv run acclaim.py pull --source pulitzer`.

- **Private Network Access.** Chrome preflights a public HTTPS origin's POST to
  `127.0.0.1` and drops it unless the response carries
  `Access-Control-Allow-Private-Network: true`, with no error on either side.
  `tools/collector.py` sends the header.
- **The 45s CDP timeout.** Fetch several pages with `Promise.all`, not one
  after another, then parse.
- Where a site's CSP or PNA blocks the POST, save the JSON another way: a blob
  download (needs downloads allowed for the site) or copying it out by hand.

## Per source

| Source | Harvest file | Shape | Notes |
|---|---|---|---|
| `pulitzer` | `harvest/pulitzer.json` | `[{category, year, status, raw, person_url, citation}]` | recipe below |
| `pen` | `harvest/pen.json` | `[{year, first, last, title, award, genre, location}]` | the archive table paginates client-side (no request per page), so walk all pages in-page and dedupe; POST blocked by CSP |
| `douban` | `harvest/douban/<year>.json` | `{year, books: [{title, author, rating, …}]}` | scripts get a stub page; POST and download both blocked |
| `obama` | `harvest/obama.json` | `[{year, url, books}]` | Medium posts; the loader prefers this file over its HTTP path |
| `nyt` | `harvest/nyt/<slug>.json` | `{list, year, books: [{rank, title, author, year}]}` | some titles wrap onto two lines and one carries its author in the title, so don't read the byline at a fixed line offset |
| `wsj` | `harvest/wsj/<slug>.json` | `{list, year, url, books: [{title, author, publisher}]}` | a title line, then `By <Author> \| <Publisher>` |

### Pulitzer recipe

Category pages are Drupal term ids: **218 Drama, 219 Fiction, 220 History, 222
Biography, 223 General Nonfiction, 224 Poetry** (221 is a 404; Memoir's id is
unknown). Each year is a `div.accordion-item` with the year in an
`a[href*="prize-winners-by-year/"]`; each entry is an anchor whose href contains
`/winners/` or `/finalists/`, and the winner's citation is `.winner-citation`.

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
