#!/usr/bin/env python3
"""
patch_nightly_twoway.py — run once.

Adds the Two-Way Tracker's step to the nightly, after the G League:
G League transactions, the day's two-way list, last night's NBA active lists,
then the tracker file. A failure there is logged but doesn't mark the
whole night failed, since gleague.nba.com being down shouldn't count as the
database failing.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "ingest_twoway.py" in src:
    print("  nightly2.py already patched")
    sys.exit()

OLD = '''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season])
'''
NEW = '''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season])

        # Two-Way Tracker: G League transactions, the two-way list, NBA active lists
        log("two-way tracker")
        if not run(log, ["ingest_twoway.py", "run", "--season", season]):
            log("two-way tracker step failed — the rest of the night carries on")
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: two-way tracker runs after the G League")
