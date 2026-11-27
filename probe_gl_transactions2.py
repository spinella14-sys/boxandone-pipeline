#!/usr/bin/env python3
"""
probe_gl_transactions2.py — one-off, writes nothing to the database.

/transactions is an article shell with no moves in it. G League seasons have
had their own pages (2023-24-nba-g-league-transactions, 22-23-...), so this
tries the likely names for this season and last, and shows what the shell's
own content holds.

    python3 probe_gl_transactions2.py
"""
import json
import os
import re

from curl_cffi import requests as cr

OUT = os.path.expanduser("~/boxandone/probe")
WORDS = ("was assigned", "was recalled", "Called Up", "was waived")


def get(url):
    try:
        r = cr.get(url, impersonate="chrome", timeout=40)
        return r.status_code, r.text
    except Exception as e:
        return str(e)[:60], ""


# 1. what the shell's content holds
html = open(os.path.join(OUT, "gl_transactions.html")).read()
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
c = json.loads(m.group(1))["props"]["pageProps"]["page"]["props"].get("content", {})
print("  shell content keys:", list(c)[:30])
for k, v in c.items():
    if isinstance(v, str) and len(v) > 40:
        print("    %-16s %s" % (k, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v))[:300]))
links = re.findall(r'href=\\?"(https?://gleague\.nba\.com/[^"\\]*transaction[^"\\]*)', json.dumps(c))
print("  transaction links in content:", sorted(set(links))[:20])

# 2. season pages
print()
for slug in ("2026-27-nba-g-league-transactions", "26-27-nba-g-league-transactions",
             "2026-27-g-league-transactions", "nba-g-league-transactions-2026-27",
             "2025-26-nba-g-league-transactions", "25-26-nba-g-league-transactions",
             "2024-25-nba-g-league-transactions", "24-25-nba-g-league-transactions",
             "2023-24-nba-g-league-transactions"):
    code, h = get("https://gleague.nba.com/" + slug)
    counts = "  ".join("%s %d" % (w.split()[-1], h.count(w)) for w in WORDS)
    dates = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{2})\b", h)
    print("  %-40s %s  %7d bytes  %s  newest date %s" % (slug, code, len(h), counts, dates[0] if dates else "-"))
    if code == 200 and h.count("was assigned"):
        open(os.path.join(OUT, slug + ".html"), "w").write(h)
