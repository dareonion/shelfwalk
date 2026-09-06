#!/usr/bin/env python3
"""Localhost sink for browser-side scrapes.

Pages that fingerprint-block scripted HTTP clients still run our JavaScript
happily, so the browser does the fetching and POSTs the result here. Keeps
large harvests out of the conversation entirely.

POST http://127.0.0.1:8765/collect?name=<slug>  body = whatever the page built
"""
import http.server, os, urllib.parse, json, datetime

OUT = os.environ.get("ACCLAIM_HARVEST") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harvest")
os.makedirs(OUT, exist_ok=True)

class H(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        # Chrome's Private Network Access: a public HTTPS origin reaching
        # 127.0.0.1 gets a preflight even for an otherwise-simple POST, and it
        # is refused unless this header comes back. Without it the request
        # never arrives and the server looks idle rather than blocked.
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_POST(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (q.get("name", ["unnamed"])[0]).replace("/", "_")[:80]
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        path = os.path.join(OUT, f"{name}.json")
        with open(path, "wb") as fh:
            fh.write(body)
        print(f"{datetime.datetime.now():%H:%M:%S}  {name}  {len(body)}B -> {path}",
              flush=True)
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print("collector on http://127.0.0.1:8765  ->", OUT, flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
