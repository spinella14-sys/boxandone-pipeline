#!/usr/bin/env python3
"""patch_nightly_dpm.py — run once: the nightly also rebuilds DPM (player files for recent games only)."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "compute_dpm.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["compute_prior_rapm.py", "build", "--season", season, "--luck"])
'''
NEW = OLD + '''                    # DPM, the daily plus-minus, carried forward through last night
                    ok &= run(log, ["compute_dpm.py", "build", "--recent"], timeout=2400)
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — run patch_nightly_lprapm.py first")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: DPM rebuilt nightly")
