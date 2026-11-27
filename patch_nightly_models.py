#!/usr/bin/env python3
"""patch_nightly_models.py — run once: the nightly also refits tracking-prior RAPM and
recomputes this season's shot quality, after DPM."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "compute_tprapm.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["compute_dpm.py", "build", "--recent"], timeout=2400)
'''
NEW = OLD + '''                    # RAPM with a box + tracking prior; shot quality and shot-making
                    ok &= run(log, ["compute_tprapm.py", "build", "--season", season])
                    ok &= run(log, ["compute_shotq.py", "build", "--seasons", season])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — run patch_nightly_dpm.py first")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: tracking-prior RAPM and shot quality nightly")
