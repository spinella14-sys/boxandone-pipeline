#!/usr/bin/env python3
"""
patch_nightly_service.py — run once.

Adds two-way eligibility to the nightly, after the two-way tracker: everyone's
Years of Service, two-way seasons by team, and whether they can sign a
two-way (build_service.py build). Its failure doesn't fail the night.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "build_service.py" in src:
    print("  nightly2.py already patched")
    sys.exit()
OLD = '''            log("two-way tracker step failed — the rest of the night carries on")
'''
NEW = '''            log("two-way tracker step failed — the rest of the night carries on")
        # Years of Service and two-way eligibility, for everyone
        if not run(log, ["build_service.py", "build"]):
            log("eligibility step failed — the rest of the night carries on")
'''
if src.count(OLD) != 1:
    sys.exit("  nightly2.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  nightly2.py patched: two-way eligibility built nightly")
