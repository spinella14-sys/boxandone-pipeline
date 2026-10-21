#!/usr/bin/env python3
"""
patch_lineups_2019c.py — run once, after patch_lineups_2019b.py.

The diagnostic showed the 2019-20 feed names the incoming player by surname
only: "Booker replaced by Johnson" (Tyler or Cameron), "Hill replaced by
Lopez" (Brook or Robin). When both are on the bench, "the one not on the
floor" cannot choose. Two more rules, in order:

  1. a suffix in the name decides: "Jackson Jr." is Jaren Jackson Jr., not
     Josh Jackson (the suffix was being stripped before it could help)
  2. look ahead: the player coming in is whichever candidate next does
     something on the floor (a shot, rebound, foul, turnover...) or is next
     subbed out, before the other is subbed in. If neither appears again in
     the period, the one with more box-score minutes still unaccounted for.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "build_lineups.py")
src = open(path).read()
if "_lookahead" in src:
    print("already patched")
    sys.exit(0)

# 1. suffix tiebreak, inside split_old_subs
old = '''        if not cands:
            v["sub_in_unresolved"] += 1'''
new = '''        if len(cands) > 1:
            raw = (desc.split("replaced by", 1)[1]).lower().replace(".", " ").split()
            suf = {w for w in raw if w in _SUFFIX}
            if suf:
                fam = {p[0]: set((pl.get("familyName") or "").lower().replace(".", " ").split())
                       for side in (home, away) for pl in side["players"] for p in [(pl["personId"],)]}
                keep = [p for p in cands if suf & fam.get(p[0], set())]
                if keep:
                    cands = keep
        if not cands:
            v["sub_in_unresolved"] += 1'''
if src.count(old) != 1:
    sys.exit("split_old_subs not as expected — nothing written")
src = src.replace(old, new)

# 2. look-ahead helper
helper = '''

def _lookahead(pa, i, cands, tid):
    """The candidate who next acts on the floor or is next subbed out."""
    for b in pa[i + 1:]:
        t = (b.get("actionType") or "").lower()
        if t == "substitution":
            if b.get("teamId") == tid:
                if (b.get("subType") or "").lower() == "out" and b.get("personId") in cands:
                    return b["personId"]
                if (b.get("subType") or "").lower() == "in" and set(b.get("candidates") or []) == set(cands):
                    return None          # the other one comes in next: no evidence either way
            continue
        hit = [p for p in actors(b) if p in cands]
        if hit:
            return hit[0]
    return None


def walk(gid, pbp, box):'''
src = src.replace("\n\ndef walk(gid, pbp, box):", helper, 1)

old = '''        st = new_stint(per, base, on)
        for a in pa:
'''
new = '''        st = new_stint(per, base, on)
        for i, a in enumerate(pa):
'''
if src.count(old) != 1:
    sys.exit("walk loop not as expected — nothing written")
src = src.replace(old, new)

old = '''                    cands = a.get("candidates") or [pid]
                    off = [c for c in cands if c not in on[tid]]
                    if len(cands) > 1 and len(off) != 1:
                        v["sub_in_ambiguous"] += 1
                    on[tid].add(off[0] if off else pid)'''
new = '''                    cands = a.get("candidates") or [pid]
                    off = [c for c in cands if c not in on[tid]]
                    pick = off[0] if off else pid
                    if len(off) > 1:
                        pick = _lookahead(pa, i, off, tid)
                        if pick is None:
                            pick = max(off, key=lambda c: box_min.get(c, 0) - played.get(c, 0))
                            v["sub_in_by_minutes"] += 1
                    elif len(cands) > 1 and not off:
                        v["sub_in_ambiguous"] += 1
                    on[tid].add(pick)'''
if src.count(old) != 1:
    sys.exit("substitution branch not as expected — nothing written")
src = src.replace(old, new)
open(path, "w").write(src)
print("build_lineups.py patched — suffix tiebreak and look-ahead for same-surname substitutions")
