#!/usr/bin/env python3
"""
patch_lineups_2019b.py — run once, after patch_lineups_2019.py.

68 early 2019-20 games still had "replaced by" names that could not be placed.
Two causes this fixes:

  - suffixes and punctuation: "Oubre Jr.", "Jackson Jr.", "Nance Jr." and
    names with apostrophes or hyphens are compared with those stripped
  - two players sharing a surname (the Antetokounmpos, the Morrises, the
    Holidays): the incoming player must be the one NOT on the floor at that
    moment, so the choice is made when the substitution happens

Whatever still cannot be placed is listed by name after each season's build,
so the remaining cases can be read rather than guessed at.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "build_lineups.py")
src = open(path).read()
if "_SUFFIX" in src:
    print("already patched")
    sys.exit(0)

start = src.index("def _fold(s):")
end = src.index("def walk(gid, pbp, box):")
new_helpers = '''_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def _fold(s):
    """Lower case, no accents, letters and spaces only, no Jr./III suffix."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = "".join(c if c.isalpha() or c == " " else " " for c in s)
    words = [w for w in s.split() if w not in _SUFFIX]
    return " ".join(words)


def split_old_subs(acts, home, away, v):
    """Early 2019-20: "X replaced by Y" rows with no separate "in" row.
    Each gets an "in" row whose candidates are the team's players matching
    the name; walk() picks the one who is off the floor at that moment."""
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
        words = who.split()
        cands = [p for p in roster.get(a.get("teamId"), [])
                 if p[3] == who or p[1] == who
                 or (p[1] and len(words) > 1 and " ".join(words[1:]) == p[1] and words[0][:1] == p[2][:1])]
        if not cands and words:
            # last resort: the last word of the name is the surname
            cands = [p for p in roster.get(a.get("teamId"), []) if p[1].split()[-1:] == words[-1:]]
        if not cands:
            v["sub_in_unresolved"] += 1
            v.setdefault("unplaced", Counter())[desc.split("replaced by", 1)[1].strip()] += 1
            continue
        ins = dict(a)
        ins["subType"] = "in"
        ins["personId"] = cands[0][0]
        ins["candidates"] = [p[0] for p in cands]
        out.append(ins)
    return out


'''
src = src[:start] + new_helpers + src[end:]

old = '''                else:
                    on[tid].add(pid)
                st = new_stint(per, at, on)'''
new = '''                else:
                    # a shared surname: the one coming in is the one not on the floor
                    cands = a.get("candidates") or [pid]
                    off = [c for c in cands if c not in on[tid]]
                    if len(cands) > 1 and len(off) != 1:
                        v["sub_in_ambiguous"] += 1
                    on[tid].add(off[0] if off else pid)
                st = new_stint(per, at, on)'''
if src.count(old) != 1:
    sys.exit("substitution branch not as expected — nothing written")
src = src.replace(old, new)

old = '''    if v["sub_in_unresolved"]:'''
new = '''    if v["sub_in_ambiguous"]:
        reasons.append("%d substitutions between same-surname players" % v["sub_in_ambiguous"])
    if v["sub_in_unresolved"]:'''
if src.count(old) != 1:
    sys.exit("validation block not as expected — nothing written")
src = src.replace(old, new)

# carry the unplaced names out to the season summary
old = '''           "reliable": not reasons, "reasons": "; ".join(reasons)}'''
new = '''           "reliable": not reasons, "reasons": "; ".join(reasons),
           "unplaced": ", ".join(sorted((v_pre.get("unplaced") or {}).keys()))}'''
if src.count(old) != 1:
    sys.exit("validation row not as expected — nothing written")
src = src.replace(old, new)

old = '''        bad = [v for v in vals if not v["reliable"]]'''
new = '''        names = Counter()
        for v in vals:
            for n in (v.get("unplaced") or "").split(", "):
                if n:
                    names[n] += 1
        if names:
            print("      names still unplaced: %s" % ", ".join("%s (%d)" % kv for kv in names.most_common(15)))
        bad = [v for v in vals if not v["reliable"]]'''
if src.count(old) != 1:
    sys.exit("season summary not as expected — nothing written")
src = src.replace(old, new)
open(path, "w").write(src)
print("build_lineups.py patched — suffixes, punctuation and shared surnames handled")
