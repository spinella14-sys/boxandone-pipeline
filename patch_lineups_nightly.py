#!/usr/bin/env python3
"""
patch_lineups_nightly.py — run once. After the nightly job writes the night's
play-by-play files, it rebuilds the current season's lineups: it fetches any
of the season's CDN games not on the runner (a fresh GitHub runner may lack
earlier nights' files, and a partial season must never overwrite a full one),
then rebuilds stints and player measures and uploads them.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(path).read()
if "build_lineups.py" in src:
    print("already patched")
    sys.exit(0)
old = '''                    ok &= run(log, ["export_game_files.py", "build",
                                    "--games", ",".join(new_games)])
'''
new = old + '''                # lineups, stints and on/off for the season so far
                if not a.no_pbp:
                    ok &= run(log, ["build_lineups.py", "fetch", "--seasons", season])
                    ok &= run(log, ["build_lineups.py", "build", "--seasons", season])
'''
if src.count(old) != 1:
    sys.exit("expected text not found — nothing written")
open(path, "w").write(src.replace(old, new))
print("nightly2.py patched — current-season lineups rebuild after each night's games")
