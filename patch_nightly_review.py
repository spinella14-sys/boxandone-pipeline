#!/usr/bin/env python3
"""
patch_nightly_review.py — run once.

Adds the Review tool's two steps to the nightly:
  - before new players are copied to Supabase: apply the decisions made in
    /admin/review, so a player created there can be tagged in reports tonight
  - after the two-way step: rebuild the queue, with whatever tonight's G
    League and RealGM runs could not match
Neither step can fail the night.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "review_queue.py" in src:
    print("  nightly2.py already patched")
    sys.exit()

PAIRS = [
    ('''        # new players must exist in Supabase before a scout can tag them
        ok &= run(log, ["sync_players_supabase.py"])''',
     '''        # names settled in the Review tool, before new players go to Supabase
        if not run(log, ["review_queue.py", "apply"]):
            log("review step failed — the rest of the night carries on")
        # new players must exist in Supabase before a scout can tag them
        ok &= run(log, ["sync_players_supabase.py"])'''),
    ('''            log("two-way tracker step failed — the rest of the night carries on")
''',
     '''            log("two-way tracker step failed — the rest of the night carries on")
        # the Review tool's queue, with whatever tonight could not match
        if not run(log, ["review_queue.py", "export"]):
            log("review queue export failed — the rest of the night carries on")
'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  nightly2.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  nightly2.py patched: review decisions applied, queue rebuilt")
