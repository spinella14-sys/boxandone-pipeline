#!/usr/bin/env python3
"""
probe_gl_logos.py — one-off, writes nothing.

Only 3 G League logos came from stats.gleague.nba.com by our team codes. The
league's own site shows every team's logo, so this lists the logo image
addresses on its pages, to find the pattern behind them.

    python3 probe_gl_logos.py
"""
import json
import os
import re

from curl_cffi import requests as cr

OUT = os.path.expanduser("~/boxandone/probe")
os.makedirs(OUT, exist_ok=True)
found = {}
for url in ("https://gleague.nba.com/", "https://gleague.nba.com/teams", "https://gleague.nba.com/standings",
            "https://stats.gleague.nba.com/teams/"):
    try:
        r = cr.get(url, impersonate="chrome", timeout=40)
    except Exception as e:
        print("  %s: %s" % (url, str(e)[:60]))
        continue
    h = r.text.replace("\\u002F", "/").replace("\\/", "/")
    open(os.path.join(OUT, "logos_" + re.sub(r"\W+", "_", url)[-30:] + ".html"), "w").write(h)
    urls = set(re.findall(r'https?://[^"\'\s<>()]+?\.(?:svg|png|webp)(?:\?[^"\'\s<>()]*)?', h, re.I))
    logos = sorted(u for u in urls if re.search(r"logo|team", u, re.I))
    print("\n  %s — HTTP %s, %d image addresses, %d look like team logos" % (url, r.status_code, len(urls), len(logos)))
    for u in logos[:40]:
        print("    " + u[:170])
        found[u] = url

# stats.gleague by a few alternative code shapes, for one team we missed
print("\n  stats.gleague codes for Santa Cruz:")
for code in ("SCW", "SC", "SAN", "SCZ", "santa-cruz", "1612709890"):
    u = "https://stats.gleague.nba.com/media/img/teams/logos/%s_logo.svg" % code
    try:
        r = cr.get(u, impersonate="chrome", timeout=20)
        print("    %-12s HTTP %s %d bytes" % (code, r.status_code, len(r.content or b"")))
    except Exception as e:
        print("    %-12s %s" % (code, str(e)[:50]))
