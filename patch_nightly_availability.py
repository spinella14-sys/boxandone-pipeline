#!/usr/bin/env python3
"""patch_nightly_availability.py — run once: the nightly also writes this season's
MIN% and qualifying pool (export_availability.py), after shot quality."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "export_availability.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''                    ok &= run(log, ["compute_shotq.py", "build", "--seasons", season])
'''
NEW = OLD + '''                    # MIN% and the qualifying pool every ranking uses
                    ok &= run(log, ["export_availability.py", "--seasons", season])
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — run patch_nightly_models.py first")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: MIN% and the qualifying pool nightly")
