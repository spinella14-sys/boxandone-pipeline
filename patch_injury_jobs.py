#!/usr/bin/env python3
"""
patch_injury_jobs.py — run once, after the injury backfill has been loaded.

  nightly2.py   reads yesterday's injury reports (each game's final list)
                and today's latest, after the eligibility step
  hourly.py     on GitHub, from 11am to midnight Eastern, re-reads today's
                latest report, so the scoreboard shows who is out tonight;
                the Mac's once-a-day run reads yesterday and today. A changed
                list counts as a change, so the database is pushed.

A failure in either step is logged and the rest of the run carries on.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected (%r) — nothing written" % (name, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


patch("nightly2.py", [(
    '''        if not run(log, ["build_service.py", "build"]):
            log("eligibility step failed — the rest of the night carries on")''',
    '''        if not run(log, ["build_service.py", "build"]):
            log("eligibility step failed — the rest of the night carries on")
        # the league's injury report: each game's final list from yesterday,
        # and today's latest
        log("injury report")
        if not run(log, ["ingest_injuries.py", "daily"], timeout=900):
            log("injury report step failed — the rest of the night carries on")''')],
    "ingest_injuries.py")

patch("hourly.py", [
    ('''        """SELECT txn_id FROM gl_transactions WHERE season = ? ORDER BY 1""",''',
     '''        """SELECT txn_id FROM gl_transactions WHERE season = ? ORDER BY 1""",
        """SELECT game_date, team_abbr, player_name, status, reason FROM injury_rows
           WHERE season = ? ORDER BY ALL""",'''),
    ('''        # season-to-date totals from stats.nba.com: catch up once a day''',
     '''        # the injury report: on GitHub today's latest, from 11am to midnight
        # Eastern; on the Mac's daily run, yesterday's finals and today
        if os.environ.get("GITHUB_RUN_ID"):
            hour = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).hour
            if 11 <= hour <= 23 and not N.run(log, ["ingest_injuries.py", "daily", "--today"], timeout=600):
                log("injury report step failed — the rest of the hour carries on")
        elif not N.run(log, ["ingest_injuries.py", "daily"], timeout=900):
            log("injury report step failed — the rest of the run carries on")
        # season-to-date totals from stats.nba.com: catch up once a day'''),
], "ingest_injuries.py")

# GitHub's runner installs from requirements.txt: the reader needs pdfplumber
req = os.path.join(HOME, "requirements.txt")
lines = open(req).read().splitlines()
if not any(l.strip().lower().startswith("pdfplumber") for l in lines):
    with open(req, "a") as f:
        f.write(("" if not lines or lines[-1] == "" else "\n") + "pdfplumber\n")
    print("  requirements.txt: pdfplumber added")
else:
    print("  requirements.txt already lists pdfplumber")
