#!/usr/bin/env python3
"""
patch_nightly_rapm.py — run once. The nightly refits RAPM for the season under
way, after the lineups are built. The 3-year window reads the two earlier
seasons' stints from R2 (compute_rapm.py copies them there on a laptop build).
"""
import os
import sys

PATH = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(PATH).read()
if "compute_rapm.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
old = '''                    if new_games:
                        ok &= run(log, ["export_game_lineups.py", "build",
                                        "--games", ",".join(new_games)])
'''
new = '''                    if new_games:
                        ok &= run(log, ["export_game_lineups.py", "build",
                                        "--games", ",".join(new_games)])
                    # RAPM for the season so far (F5)
                    ok &= run(log, ["compute_rapm.py", "build", "--season", season])
'''
if src.count(old) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(old, new))
print("  nightly2.py patched")
