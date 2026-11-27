#!/usr/bin/env python3
"""diag_2019.py — why do early 2019-20 games fail the lineup check? Reads only."""
import os, sys
sys.path.insert(0, os.path.expanduser("~/boxandone"))
import cdn_feed as C
for gid in ("0021900011", "0021900464"):          # one failing, one passing
    box, pbp = C.load(gid, "box", fetch=False), C.load(gid, "pbp", fetch=False)
    print("\n%s  box %s  pbp %s" % (gid, bool(box), bool(pbp)))
    if not box or not pbp:
        continue
    for side in ("homeTeam", "awayTeam"):
        t = box["game"][side]
        ps = t["players"]
        print("  %s teamId %s  players %d  starters %s" % (t.get("teamTricode"), t.get("teamId"), len(ps),
              [p.get("personId") for p in ps if str(p.get("starter")) == "1"]))
        p = ps[0]
        print("    e.g. %s" % {k: p.get(k) for k in ("personId", "name", "starter", "played", "status")},
              "minutes=%r" % (p.get("statistics") or {}).get("minutes"))
    acts = pbp["game"]["actions"]
    subs = [a for a in acts if a.get("actionType") == "substitution"][:3]
    print("  actions %d, first substitutions:" % len(acts))
    for a in subs:
        print("    %s" % {k: a.get(k) for k in ("period", "clock", "subType", "personId", "teamId", "description")})
    shot = next(a for a in acts if a.get("actionType") in ("2pt", "3pt"))
    print("  first shot: %s" % {k: shot.get(k) for k in ("personId", "teamId", "teamTricode", "description")})
