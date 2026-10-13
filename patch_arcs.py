#!/usr/bin/env python3
"""
patch_arcs.py — run once. After the nightly job rewrites a player's career
game log, it now recomputes his career-arc points (compute_arcs.py) from it.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(path).read()
if "compute_arcs.py" in src:
    print("already patched")
    sys.exit(0)
old = "            export_changed_players(env, log, season, pids)\n"
new = old + '''            # career-arc points read the game logs just written
            if pids:
                ok &= run(log, ["compute_arcs.py", "build", "--players", ",".join(pids)])
'''
if src.count(old) != 1:
    sys.exit("expected text not found — nothing written")
open(path, "w").write(src.replace(old, new))
print("nightly2.py patched — arcs recompute for every changed player")
