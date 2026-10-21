#!/usr/bin/env python3
"""
patch_lineups_2019.py — run once.

Early 2019-20 games record a substitution as a single row, "O'Neale replaced
by Ingles", with only the outgoing player's id. There is no "in" row, so the
incoming player never entered our lineup and the game fell apart (0 of 20
minutes matching). Now, in a game with no "in" rows at all, each "replaced by"
row gets a matching "in" for the named player, found on that team's box score
by surname (first initial when two share one). A name that cannot be placed
is counted, and the game is set aside rather than guessed.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "build_lineups.py")
src = open(path).read()
if "replaced by" in src:
    print("already patched")
    sys.exit(0)

anchor = '''    acts = sorted(pbp["game"]["actions"], key=lambda a: (a.get("orderNumber") or 0))
'''
add = '''    acts = split_old_subs(acts, home, away, v_pre := Counter())
'''
helper = '''

def _fold(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().replace(".", "").strip()


def split_old_subs(acts, home, away, v):
    """Early 2019-20: "X replaced by Y" rows with no separate "in" row."""
    subs = [a for a in acts if (a.get("actionType") or "").lower() == "substitution"]
    if not subs or any((a.get("subType") or "").lower() == "in" for a in subs):
        return acts
    roster = {}
    for side in (home, away):
        roster[side["teamId"]] = [(p["personId"], _fold(p.get("familyName")), _fold(p.get("firstName")),
                                   _fold(p.get("name"))) for p in side["players"]]
    out = []
    for a in acts:
        out.append(a)
        if (a.get("actionType") or "").lower() != "substitution":
            continue
        desc = a.get("description") or ""
        if "replaced by" not in desc:
            continue
        who = _fold(desc.split("replaced by", 1)[1])
        cands = [p for p in roster.get(a.get("teamId"), []) if p[3] == who or p[1] == who
                 or (p[1] and who.endswith(" " + p[1]) and who[:1] == p[2][:1])]
        if len(cands) > 1:
            # two players share the surname: the description's initial decides
            cands = [p for p in cands if p[3] == who or who.split(" ")[0][:1] == p[2][:1]]
        if len(cands) != 1:
            v["sub_in_unresolved"] += 1
            continue
        ins = dict(a)
        ins["subType"] = "in"
        ins["personId"] = cands[0][0]
        out.append(ins)
    return out
'''
if src.count(anchor) != 1 or "def walk(gid, pbp, box):" not in src:
    sys.exit("build_lineups.py not as expected — nothing written")
src = src.replace(anchor, anchor + add)
src = src.replace("def walk(gid, pbp, box):", helper.lstrip("\n") + "\n\ndef walk(gid, pbp, box):", 1)
# carry the unresolved count into the game's validation
old = '''    reasons = []
    if v["off_floor"]:'''
new = '''    v.update(v_pre)
    reasons = []
    if v["sub_in_unresolved"]:
        reasons.append("%d substitutions with an unplaceable incoming name" % v["sub_in_unresolved"])
    if v["off_floor"]:'''
if src.count(old) != 1:
    sys.exit("validation block not as expected — nothing written")
src = src.replace(old, new)
open(path, "w").write(src)
print("build_lineups.py patched — early 2019-20 'replaced by' substitutions now enter the lineup")
