#!/usr/bin/env python3
"""
patch_nightly_prapm.py — run once.

After the nightly refits RAPM for the season, it refits box-prior RAPM too
(compute_prior_rapm.py), so the Models tab stays current.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "compute_prior_rapm.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["compute_rapm.py", "build", "--season", season])
'''
NEW = OLD + '''                    # RAPM with a box-score prior, for the Models tab
                    ok &= run(log, ["compute_prior_rapm.py", "build", "--season", season])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: box-prior RAPM refitted after RAPM")
