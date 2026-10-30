#!/usr/bin/env python3
"""
patch_nightly_team_shots.py — run once. After RAPM, the nightly rewrites the
current season's team shot files (export_team_shots.py, hot database only),
so team pages show last night's games on both ends.
"""
import os
import sys

PATH = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(PATH).read()
if "export_team_shots.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
old = '''                    # RAPM for the season so far (F5)
                    ok &= run(log, ["compute_rapm.py", "build", "--season", season])
'''
new = old + '''                    # each team's shots, both ends, for the team page
                    ok &= run(log, ["export_team_shots.py", "build", "--season", season, "--hot"])
'''
if src.count(old) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(old, new))
print("  nightly2.py patched")
