#!/usr/bin/env python3
"""
patch_nightly_playtypes.py — run once.

Adds two steps to nightly2.py:
  - ingest_playtypes.py for the season, right after ingest_tracking.py. It is
    stats.nba.com, so it runs from the Mac and is skipped on GitHub, the same
    as tracking. About 70 requests, two to three minutes.
  - export_game_lineups.py for the night's new games, right after the
    season's lineups are rebuilt, so each game page gets its units.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(path).read()
if "ingest_playtypes.py" in src:
    print("already patched")
    sys.exit(0)

old = '''"bridge_nba_ids.py", "ingest_tracking.py"}'''
new = '''"bridge_nba_ids.py", "ingest_tracking.py",
               "ingest_playtypes.py"}'''
if src.count(old) != 1:
    sys.exit("STATS_STEPS not as expected — nothing written")
src = src.replace(old, new)

old = '''            ok &= run(log, ["ingest_tracking.py", "build", "--season", season])
'''
new = old + '''            # play types, closest-defender shooting, hustle
            ok &= run(log, ["ingest_playtypes.py", "build", "--season", season])
'''
if src.count(old) != 1:
    sys.exit("tracking step not as expected — nothing written")
src = src.replace(old, new)

old = '''                    ok &= run(log, ["build_lineups.py", "build", "--seasons", season])
'''
new = old + '''                    if new_games:
                        ok &= run(log, ["export_game_lineups.py", "build",
                                        "--games", ",".join(new_games)])
'''
if src.count(old) != 1:
    sys.exit("lineups step not as expected — nothing written")
src = src.replace(old, new)
open(path, "w").write(src)
print("nightly2.py patched — play types and per-game lineups added")
