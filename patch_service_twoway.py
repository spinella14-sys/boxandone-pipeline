#!/usr/bin/env python3
"""patch_service_twoway.py — run once: a season on a two-way now counts as a Year of
Service, active games or not (Adam's correction). Then rebuild eligibility."""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "build_service.py")
src = open(PATH).read()
if "TWO_WAY_EARNS_YOS = True" in src:
    print("  build_service.py already patched")
    sys.exit()
EDITS = [
    ("TWO_WAY_EARNS_YOS = False", "TWO_WAY_EARNS_YOS = True"),
    ("ONE CHOICE, MADE ON ADVICE\n", "ONE CHOICE, CORRECTED\n"),
    ("  league contact. Hoops Rumors writes as though it does. TWO_WAY_EARNS_YOS\n"
     "  holds the choice; every result says which rule produced it.",
     "  league contact — later corrected: a season on a two-way uses a season of\n"
     "  eligibility whether or not he was ever active, as Hoops Rumors has it.\n"
     "  TWO_WAY_EARNS_YOS holds the choice; each season records how it counted."),
]
for old, new in EDITS:
    if src.count(old) != 1:
        sys.exit("  build_service.py not as expected near: %r" % old[:50])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  build_service.py patched: two-way seasons count as Years of Service")
