#!/usr/bin/env python3
"""diag_2019b.py — what do the 'same-surname' substitutions in 2019-20 look like? Reads only."""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.expanduser("~/boxandone"))
import cdn_feed as C          # noqa: E402
import build_lineups as B     # noqa: E402

for gid in ("0021900012", "0021900015", "0021900020"):
    pbp, box = C.load(gid, "pbp", fetch=False), C.load(gid, "box", fetch=False)
    g = box["game"]
    names = {p["personId"]: "%s %s" % (p.get("firstName"), p.get("familyName"))
             for s in ("homeTeam", "awayTeam") for p in g[s]["players"]}
    acts = sorted(pbp["game"]["actions"], key=lambda a: a.get("orderNumber") or 0)
    out = B.split_old_subs(acts, g["homeTeam"], g["awayTeam"], Counter())
    print("\n%s  %s vs %s" % (gid, g["awayTeam"]["teamTricode"], g["homeTeam"]["teamTricode"]))
    shown = 0
    for a in out:
        if a.get("subType") == "in" and len(a.get("candidates") or []) > 1 and shown < 6:
            shown += 1
            print("  %s  %-55s -> %s" % (a.get("clock"), (a.get("description") or "")[:55],
                                         [names.get(c, c) for c in a["candidates"]]))
    if not shown:
        print("  no substitution with more than one candidate")
