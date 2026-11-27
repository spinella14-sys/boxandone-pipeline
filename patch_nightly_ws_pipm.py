#!/usr/bin/env python3
"""patch_nightly_ws_pipm.py — run once: the nightly also computes this season's Win
Shares and PIPM-style rating, after MIN%."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "compute_ws.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["export_availability.py", "--seasons", season])
'''
NEW = OLD + '''                    # Win Shares; box score plus luck-adjusted on/off (PIPM-style)
                    ok &= run(log, ["compute_ws.py", "build", "--seasons", season])
                    ok &= run(log, ["compute_pipm.py", "build", "--seasons", season])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — run patch_nightly_availability.py first")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: Win Shares and PIPM-style nightly")
