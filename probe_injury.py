#!/usr/bin/env python3
"""
probe_injury.py — one-off, writes nothing to the database.

What the NBA's injury-report archive actually holds, before building on it:

  1. how far back the PDFs go, and how their addresses are written in each
     era (hourly "_05PM" in older seasons, quarter-hourly "_05_15PM" lately)
  2. what one report looks like inside, from each era — the columns, how a
     row with a blank game/team cell carries down, the reason text
  3. whether GitHub's side could reach them (answered later; this runs on the Mac)

PDFs found are saved to ~/boxandone/probe/injury/ so the reader can be built
against real files.

    pip3 install --user pdfplumber      # once, if it isn't installed
    python3 probe_injury.py
"""
import os
import sys
from datetime import date

from curl_cffi import requests as cr

OUT = os.path.expanduser("~/boxandone/probe/injury")
os.makedirs(OUT, exist_ok=True)
BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_%s_%s.pdf"

# a mid-season game day in every season back to 2015-16, plus a few more
DAYS = [date(2015, 12, 1), date(2016, 12, 1), date(2017, 12, 1), date(2018, 12, 1), date(2019, 12, 2),
        date(2020, 12, 26), date(2021, 2, 1), date(2021, 10, 20), date(2021, 12, 1), date(2022, 12, 1),
        date(2023, 12, 1), date(2024, 12, 2), date(2025, 2, 1), date(2025, 10, 26), date(2025, 12, 1),
        date(2026, 2, 25), date(2026, 11, 18)]


def stamps():
    """Report times worth trying, evening first: hourly and quarter-hourly forms."""
    out = []
    for h in (5, 6, 7, 8, 4, 1, 12, 11, 10, 9):
        ap = "AM" if h in (9, 10, 11) else "PM"
        out.append("%02d%s" % (h, ap))
        for m in ("00", "15", "30", "45"):
            out.append("%02d_%s%s" % (h, m, ap))
    return out


def exists(url):
    try:
        r = cr.head(url, impersonate="chrome", timeout=15)
        return r.status_code == 200
    except Exception:
        return False


found = {}
print("  1. which days have reports, and in which address form\n")
for d in DAYS:
    hits = []
    for st in stamps():
        u = BASE % (d.isoformat(), st)
        if exists(u):
            hits.append(st)
            if len(hits) >= 3:
                break
    print("    %s  %s" % (d, ", ".join(hits) if hits else "none found"))
    if hits:
        found[d] = BASE % (d.isoformat(), hits[0])

print("\n  2. inside a report from each era\n")
try:
    import pdfplumber
except ImportError:
    sys.exit("    pdfplumber isn't installed — run:  pip3 install --user pdfplumber   then this again")

seen_years = set()
for d, url in found.items():
    era = d.year if d.month >= 7 else d.year - 1
    if era in seen_years and d != max(found):
        continue
    seen_years.add(era)
    p = os.path.join(OUT, os.path.basename(url))
    r = cr.get(url, impersonate="chrome", timeout=30)
    open(p, "wb").write(r.content)
    with pdfplumber.open(p) as pdf:
        print("    %s — %d pages, %d bytes" % (os.path.basename(url), len(pdf.pages), len(r.content)))
        page = pdf.pages[0]
        tables = page.extract_tables()
        if tables:
            t = tables[0]
            print("      table on page 1: %d rows x %d columns" % (len(t), max(len(x) for x in t)))
            for row in t[:8]:
                print("        " + " | ".join((c or "").replace("\n", " ")[:28] for c in row))
        else:
            print("      no table found; first lines of text:")
        for line in (page.extract_text() or "").splitlines()[:14]:
            print("        > " + line[:150])
    print()

print("  3. saved %d reports to %s" % (len(os.listdir(OUT)), OUT))
