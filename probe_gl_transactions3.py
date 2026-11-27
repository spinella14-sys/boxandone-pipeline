#!/usr/bin/env python3
"""
probe_gl_transactions3.py — one-off, writes nothing to the database.

Three places G League moves might come from:
  1. the blocks/components of gleague.nba.com/transactions (a widget's feed)
  2. NBA.com's player-movement feed — carries NBA person ids
  3. RealGM's G League transaction log — unofficial, names only

    python3 probe_gl_transactions3.py
"""
import json
import os
import re
from collections import Counter

from curl_cffi import requests as cr

OUT = os.path.expanduser("~/boxandone/probe")


def get(url, **kw):
    try:
        r = cr.get(url, impersonate="chrome", timeout=40, **kw)
        return r.status_code, r.text
    except Exception as e:
        return str(e)[:60], ""


# 1. the page's blocks
html = open(os.path.join(OUT, "gl_transactions.html")).read()
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
c = json.loads(m.group(1))["props"]["pageProps"]["page"]["props"]["content"]
print("1. page blocks")
for key in ("blocks", "components"):
    v = c.get(key)
    s = json.dumps(v)
    print("  %s: %d chars" % (key, len(s)))
    print("   ", s[:900])
    for u in sorted(set(re.findall(r'https?://[^"\\\s]+', s)))[:15]:
        print("    url:", u[:160])
print("  contentText:", (c.get("contentText") or "")[:300])

# 2. NBA.com player movement
print("\n2. NBA.com player movement")
code, t = get("https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json",
              headers={"Referer": "https://www.nba.com/"})
print("  HTTP %s, %d bytes" % (code, len(t)))
try:
    d = json.loads(t)
    rows = d["NBA_Player_Movement"]["rows"]
    print("  %d rows; keys %s" % (len(rows), list(rows[0])))
    recent = [r for r in rows if str(r.get("TRANSACTION_DATE", "")) >= "2026-10-01"]
    print("  since Oct 1: %d — types %s" % (len(recent), Counter(r.get("Transaction_Type") for r in recent).most_common()))
    for w in ("Two-Way", "two-way", "assigned", "recalled", "G League", "converted"):
        hits = [r for r in recent if w in str(r.get("TRANSACTION_DESCRIPTION", ""))]
        print("    %-10s %4d  e.g. %s" % (w, len(hits), hits[0]["TRANSACTION_DESCRIPTION"][:110] if hits else ""))
    open(os.path.join(OUT, "nba_player_movement.json"), "w").write(t)
except Exception as e:
    print("  could not read:", str(e)[:80])

# 3. RealGM
print("\n3. RealGM G League transactions")
code, h = get("https://basketball.realgm.com/gleague/transactions/league")
print("  HTTP %s, %d bytes" % (code, len(h)))
txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "\n", h))
for w in ("assigned", "recalled", "Two-Way", "waived", "acquired"):
    print("    %-10s %d" % (w, txt.count(w)))
for s in re.findall(r"[A-Z][^.]{10,160}(?:assigned|recalled)[^.]{0,120}\.", txt)[:5]:
    print("    e.g.", s.strip()[:200])
if code == 200:
    open(os.path.join(OUT, "realgm_gl_transactions.html"), "w").write(h)
