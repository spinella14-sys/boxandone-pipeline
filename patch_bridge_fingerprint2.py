#!/usr/bin/env python3
"""
patch_bridge_fingerprint2.py

Corrected version. The first patch replaced everything between the FINGERPRINT
constant and resolve(), which deleted norm_team, db, fetch_gamelog and the two
row-grouping functions. This one edits only what it needs to.

Three surgical changes:
  1. FINGERPRINT loses "sec"      (BBRef keeps exact seconds, NBA.com rounds)
  2. match_game gains a minutes tiebreak for shared stat lines
  3. cmd_diagnose appended, plus the CLI choice

Run:  python3 patch_bridge_fingerprint2.py

If bridge_nba_ids.py was damaged by the first patch, restore it first:
    cp bridge_nba_ids.py.bak_fp bridge_nba_ids.py
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/bridge_nba_ids.py")

OLD_FP = '''# Fields that form the fingerprint, in priority order.
FINGERPRINT = ("sec", "pts", "trb", "ast", "fga", "fta")'''

NEW_FP = '''# Exact integers on BOTH sides. "sec" is deliberately absent: BBRef stores
# exact seconds (35:12 -> 2112) while NBA.com rounds to whole minutes
# (35 -> 2100), so any fingerprint containing it never matches.
FINGERPRINT = ("pts", "trb", "ast", "fga", "fta")
MINUTE_TOLERANCE_SEC = 90      # tiebreak only
MINUTE_SEPARATION_SEC = 30     # runner-up must be at least this much worse'''

NEW_MATCH = '''def _mins(row):
    s = row.get("sec")
    return None if s is None else s / 60.0


def match_game(ours, theirs):
    """
    Return (mappings, ambiguous_count).

    Primary key is the stat line, which is exact on both sides. When several
    players on a team share a line, fall back to minutes: pick the closest
    counterpart, and only accept when it is clearly closest.
    """
    mappings = []
    ambiguous = 0

    for team in {p["team"] for p in ours["players"]}:
        a = [p for p in ours["players"] if p["team"] == team]
        b = [p for p in theirs["players"] if p["team"] == team]
        if not a or not b:
            continue

        a_idx, b_idx = defaultdict(list), defaultdict(list)
        for p in a:
            a_idx[fp(p)].append(p)
        for p in b:
            b_idx[fp(p)].append(p)

        for key, alist in a_idx.items():
            blist = b_idx.get(key, [])

            if len(alist) == 1 and len(blist) == 1:
                mappings.append((alist[0]["player_id"], blist[0]["nba_id"],
                                 blist[0]["name"]))
                continue

            if not blist:
                ambiguous += len(alist)
                continue

            resolved = 0
            for ap in alist:
                am = _mins(ap)
                if am is None:
                    continue
                scored = []
                for bp in blist:
                    bm = _mins(bp)
                    if bm is not None:
                        scored.append((abs(am - bm) * 60.0, bp))
                if not scored:
                    continue
                scored.sort(key=lambda x: x[0])
                best_d, best_p = scored[0]
                second_d = scored[1][0] if len(scored) > 1 else None
                if best_d <= MINUTE_TOLERANCE_SEC and (
                        second_d is None or second_d >= best_d + MINUTE_SEPARATION_SEC):
                    mappings.append((ap["player_id"], best_p["nba_id"],
                                     best_p["name"]))
                    resolved += 1
            ambiguous += len(alist) - resolved

    return mappings, ambiguous


'''

DIAGNOSE = '''

def cmd_diagnose(season):
    """Print one paired game side by side and list unpaired games."""
    con = db()
    ours_all = our_rows_by_game(con, season)
    theirs_all = {}
    for st in ("Regular Season", "Playoffs"):
        theirs_all.update(nba_rows_by_game(fetch_gamelog(season, st)))

    unpaired = [k for k in ours_all if k not in theirs_all]
    print(f"\\n  local games {len(ours_all):,} | nba games {len(theirs_all):,} "
          f"| unpaired {len(unpaired)}")
    for k in sorted(unpaired)[:15]:
        print(f"    unpaired: {k[0]}  {sorted(k[1])}")

    key = next((k for k in ours_all if k in theirs_all), None)
    if not key:
        print("  no paired game to inspect")
        con.close()
        return

    ours, theirs = ours_all[key], theirs_all[key]
    team = sorted({p["team"] for p in ours["players"]})[0]
    print(f"\\n  sample game {key[0]} {sorted(key[1])} — team {team}")
    print(f"  {'--- OURS (bbref) ---':>34}    {'--- THEIRS (nba.com) ---'}")
    print(f"  {'sec':>6}{'min':>8}{'pts':>5}{'trb':>5}{'ast':>5}{'fga':>5}"
          f"    {'sec':>6}{'min':>8}{'pts':>5}{'trb':>5}{'ast':>5}{'fga':>5}  name")

    a = sorted([p for p in ours["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    b = sorted([p for p in theirs["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    for i in range(max(len(a), len(b))):
        l = a[i] if i < len(a) else {}
        r = b[i] if i < len(b) else {}
        ls, rs = l.get("sec"), r.get("sec")
        print(f"  {str(ls):>6}{(ls/60 if ls else 0):>8.2f}"
              f"{str(l.get('pts')):>5}{str(l.get('trb')):>5}"
              f"{str(l.get('ast')):>5}{str(l.get('fga')):>5}"
              f"    {str(rs):>6}{(rs/60 if rs else 0):>8.2f}"
              f"{str(r.get('pts')):>5}{str(r.get('trb')):>5}"
              f"{str(r.get('ast')):>5}{str(r.get('fga')):>5}  {r.get('name','')}")

    whole = sum(1 for p in b if p.get("sec") is not None and p["sec"] % 60 == 0)
    tot = sum(1 for p in b if p.get("sec") is not None)
    verdict = "ROUNDED — diagnosis confirmed" if tot and whole == tot else "exact seconds"
    print(f"\\n  nba.com rows landing on a whole minute: {whole}/{tot}  -> {verdict}")
    con.close()
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"{TARGET} not found")
    src = open(TARGET, encoding="utf-8").read()

    if "def db():" not in src or "def our_rows_by_game" not in src:
        sys.exit("  bridge_nba_ids.py is missing functions — restore it first:\n"
                 "    cp bridge_nba_ids.py.bak_fp bridge_nba_ids.py")

    if "MINUTE_TOLERANCE_SEC" in src:
        print("  already patched")
        return

    if OLD_FP not in src:
        sys.exit("  FINGERPRINT block not found — file differs from expected")
    src = src.replace(OLD_FP, NEW_FP)

    # replace fp() + match_game() only, bounded by the next def
    s = src.find("def fp(row):")
    e = src.find("def resolve(season, con", s)
    if s == -1 or e == -1:
        sys.exit("  could not bound fp()/match_game()")
    src = src[:s] + 'def fp(row):\n    return tuple(row.get(k) for k in FINGERPRINT)\n\n\n' \
          + NEW_MATCH + src[e:]

    # cmd_diagnose + CLI
    anchor = 'if __name__ == "__main__":'
    src = src.replace(anchor, DIAGNOSE.strip("\n") + "\n\n\n" + anchor)
    src = src.replace(
        'ap.add_argument("cmd", choices=["build", "update", "status"])',
        'ap.add_argument("cmd", choices=["build", "update", "status", "diagnose"])')
    src = src.replace(
        '    else:\n        cmd_status()',
        '    elif a.cmd == "diagnose":\n        cmd_diagnose(a.season)\n'
        '    else:\n        cmd_status()')

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit(f"  would produce invalid syntax ({e}) — nothing changed")

    for fn in ("db", "norm_team", "fetch_gamelog", "nba_rows_by_game",
               "our_rows_by_game", "resolve", "write_ids", "cmd_build",
               "cmd_update", "cmd_status", "cmd_diagnose", "match_game", "fp"):
        if f"def {fn}(" not in src:
            sys.exit(f"  sanity check failed: {fn}() missing — nothing changed")

    open(TARGET + ".bak_fp2", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  FINGERPRINT -> (pts, trb, ast, fga, fta)")
    print("  minutes demoted to tiebreak")
    print("  cmd_diagnose added")
    print("  all 13 functions verified present")
    print("  backup -> bridge_nba_ids.py.bak_fp2")


if __name__ == "__main__":
    main()
