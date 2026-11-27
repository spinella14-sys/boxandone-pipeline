#!/usr/bin/env python3
"""
patch_lineups_2019.py — run once.

2019-20's lineups hold 797 of its ~1,060 games. Two reasons, both fixed here:

  1. The 88 "seeding" games in the bubble (July–August 2020) carry regular-
     season ids past the usual 1,230 (0021901231 to 0021901318). The id guess
     stopped at 1,230, so they were never asked for. 2019-20's guess now runs
     to 1,320.
  2. A game NBA.com didn't answer for once was written to the missing list
     and never asked for again. Every 2019-20 id on that list is cleared, so
     the next fetch tries each once more; whatever is truly absent goes back
     on the list.

Then:  build_lineups.py fetch / build --seasons 2019-20, and the steps that
read the stints (the commands Claude gave).
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "build_lineups.py")
src = open(PATH).read()
OLD = '''    ids = ["002%s0%04d" % (yy, n) for n in range(1, 1231)]'''
NEW = '''    # 2019-20's bubble seeding games ran past the usual 1,230 (to 1318)
    ids = ["002%s0%04d" % (yy, n) for n in range(1, 1321 if yy == "19" else 1231)]'''
if NEW in src:
    print("  build_lineups.py already patched")
elif src.count(OLD) == 1:
    open(PATH, "w").write(src.replace(OLD, NEW))
    print("  build_lineups.py patched: 2019-20 ids run to 1320 (the bubble seeding games)")
else:
    sys.exit("  build_lineups.py not as expected — nothing written")

missing = os.path.join(HOME, "raw", "pbp_cdn", "missing.txt")
try:
    ids = open(missing).read().split()
except OSError:
    ids = []
keep = [g for g in ids if g[3:5] != "19"]
gone = len(ids) - len(keep)
if gone:
    open(missing, "w").write("\n".join(sorted(keep)))
print("  missing list: %d 2019-20 ids cleared, to be tried again" % gone)
