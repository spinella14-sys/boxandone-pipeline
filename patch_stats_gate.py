#!/usr/bin/env python3
"""
patch_stats_gate.py — run once.

GitHub's servers cannot reach stats.nba.com (every request hangs until it
times out). Each step that uses it was retrying for minutes, the hourly job hit
its 20-minute limit and was killed, and the queued nightly job was cancelled
behind it.

This makes nightly2.run() test stats.nba.com once per run (10 seconds). When
it is unreachable, the steps that need it — and the steps that only process
what they fetch — are skipped with a log line instead of hanging. Everything
from Basketball Reference and cdn.nba.com still runs.

On the Mac, stats.nba.com answers, so nothing is skipped there.
"""
import os
import re
import sys

HOME = os.path.expanduser("~/boxandone")
path = os.path.join(HOME, "nightly2.py")
src = open(path).read()

if "STATS_STEPS" in src:
    print("already patched")
    sys.exit(0)

old = "def run(log, argv, timeout=5400):\n    log(\"$ %s\" % \" \".join(argv))\n"
assert src.count(old) == 1, "run() not found as expected — not patched"

new = '''# Steps that call stats.nba.com, or only process what those calls fetched.
STATS_STEPS = {"ingest_rosters.py", "ingest_preseason.py", "bridge_games.py",
               "ingest_pbp.py", "bridge_nba_ids.py", "parse_assists.py",
               "export_game_files.py", "ingest_tracking.py"}
_stats_ok = None


def stats_reachable(log):
    """One 10-second request, once per run. GitHub's servers get no answer."""
    global _stats_ok
    if _stats_ok is None:
        try:
            from curl_cffi import requests as cr
            r = cr.get("https://stats.nba.com/stats/commonteamroster"
                       "?LeagueID=00&Season=2026-27&TeamID=1610612738",
                       headers={"Referer": "https://www.nba.com/",
                                "Origin": "https://www.nba.com",
                                "x-nba-stats-origin": "stats",
                                "x-nba-stats-token": "true"},
                       impersonate="chrome", timeout=10)
            _stats_ok = r.status_code == 200
        except Exception:
            _stats_ok = False
        log("stats.nba.com %s" % ("reachable" if _stats_ok
                                  else "UNREACHABLE — its steps will be skipped"))
    return _stats_ok


def run(log, argv, timeout=5400):
    if argv and argv[0] in STATS_STEPS and not stats_reachable(log):
        log("$ %s  — skipped (stats.nba.com unreachable)" % " ".join(argv))
        return True
    log("$ %s" % " ".join(argv))
'''
src = src.replace(old, new)
open(path, "w").write(src)
print("nightly2.py patched — stats.nba.com steps skip when it can't be reached")
