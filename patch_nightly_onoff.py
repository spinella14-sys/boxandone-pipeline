#!/usr/bin/env python3
"""
patch_nightly_onoff.py — run once.

After the nightly rebuilds the season's lineup stints, it also rebuilds the
season's on/off totals (build_onoff.py), so the team-impact columns on the
player pages stay current.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "build_onoff.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["build_lineups.py", "build", "--seasons", season])
'''
NEW = '''                    ok &= run(log, ["build_lineups.py", "build", "--seasons", season])
                    # on/off totals for both teams, for the team-impact columns
                    ok &= run(log, ["build_onoff.py", "build", "--seasons", season])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: on/off totals rebuilt after the lineups")
