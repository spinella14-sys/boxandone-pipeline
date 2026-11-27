#!/usr/bin/env python3
"""
patch_nightly_link.py — run once.

After the nightly parses box scores, any player Basketball Reference names
that we hold only by NBA id (a rookie's debut) is linked by
link_staged_bbref.py, and the box scores are parsed again so his line lands
the same night instead of being set aside.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(path).read()
if "link_staged_bbref.py" in src:
    print("already patched")
    sys.exit(0)
old = '''        ok &= run(log, ["ingest_games.py", "parse"])
'''
new = old + '''        # a debut: Basketball Reference names a player we hold only by NBA id
        ok &= run(log, ["link_staged_bbref.py", "--in-run"])
        ok &= run(log, ["ingest_games.py", "parse"])
'''
if src.count(old) != 1:
    sys.exit("parse step not as expected — nothing written")
open(path, "w").write(src.replace(old, new))
print("nightly2.py patched — debuts are linked and parsed the same night")
