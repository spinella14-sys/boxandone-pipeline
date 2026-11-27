#!/usr/bin/env python3
"""
probe_gl_transactions.py — one-off, writes nothing to the database.

gleague.nba.com/transactions came back with no moves on it: the list is filled
in by the browser after the page loads. This finds where it comes from — an
embedded data blob or a feed URL — and saves the raw page so it can be read.

    python3 probe_gl_transactions.py
"""
import json
import os
import re

from curl_cffi import requests as cr

OUT = os.path.expanduser("~/boxandone/probe")
os.makedirs(OUT, exist_ok=True)
URL = "https://gleague.nba.com/transactions"

r = cr.get(URL, impersonate="chrome", timeout=40)
html = r.text
open(os.path.join(OUT, "gl_transactions.html"), "w").write(html)
print("  HTTP %s, %d bytes, saved to probe/gl_transactions.html" % (r.status_code, len(html)))

for word in ("was assigned", "was recalled", "Called Up", "Two-Way", "waived"):
    print("  %-14s %d times in the raw page" % (word, html.count(word)))

# embedded data blobs
for name in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__APOLLO_STATE__", "window.__"):
    if name in html:
        print("  embedded blob:", name)
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
if m:
    d = json.loads(m.group(1))
    open(os.path.join(OUT, "gl_transactions_next.json"), "w").write(json.dumps(d, indent=1))
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + "." + k)
        elif isinstance(o, list):
            if o and isinstance(o[0], dict):
                print("    list %-70s %d items, keys %s" % (path[:70], len(o), list(o[0])[:8]))
            for i, v in enumerate(o[:1]):
                walk(v, path + "[0]")
    walk(d)
    print("  page props keys:", list(d.get("props", {}).get("pageProps", {}))[:20])

# URLs the page might fetch its list from
urls = set(re.findall(r'https?://[^"\'\s<>]+', html))
for u in sorted(urls):
    if re.search(r"transact|api|json|feed|graphql|wp-json|content|cms", u, re.I) and not u.endswith((".png", ".jpg", ".svg", ".woff2", ".css")):
        print("  url:", u[:160])
for s in sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))[:40]:
    print("  script:", s[:160])
