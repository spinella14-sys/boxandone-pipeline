#!/usr/bin/env python3
"""patch_nightly_lprapm.py — run once: the nightly also refits the luck-adjusted box-prior RAPM."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if '"--luck"' in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["compute_prior_rapm.py", "build", "--season", season])
'''
NEW = OLD + '''                    ok &= run(log, ["compute_prior_rapm.py", "build", "--season", season, "--luck"])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — run patch_nightly_prapm.py first")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: luck-adjusted box-prior RAPM refitted nightly")
