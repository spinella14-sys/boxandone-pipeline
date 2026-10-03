#!/usr/bin/env python3
"""
patch_bridge_fingerprint.py

The bridge matched only 335 of 589 players and reported 28,014 ambiguous
player-games. That is not ambiguity — it is a field that never matches.

Cause: BBRef stores exact time played (35:12 -> 2112 sec). NBA.com's
leaguegamelog returns rounded whole minutes (35 -> 2100 sec). The two never
agree, so every fingerprint containing seconds fails. The only players that
mapped were ones who happened to play an exact whole minute in some game —
about 57% over a full season, which is what we saw.

Fix, two parts:

  1. Fingerprint on the five fields that are exact integers on BOTH sides:
     pts, trb, ast, fga, fta.
  2. Minutes become a TIEBREAK, not part of the key. When several players on
     a team share a stat line, pick the one whose minutes are closest, and
     only if that is unambiguous within a 1-minute tolerance.

Also adds:  python3 bridge_nba_ids.py diagnose --season 2025-26
which prints one paired game side by side and lists unpaired games, so the
minute-format assumption is verified rather than trusted.

Run:  python3 patch_bridge_fingerprint.py
"""

import os
import sys

TARGET = os.path.expanduser("~/boxandone/bridge_nba_ids.py")

NEW_BLOCK = '''# Exact integers on both sides. Minutes are deliberately excluded: BBRef keeps
# exact seconds, NBA.com rounds to whole minutes, so they never agree.
FINGERPRINT = ("pts", "trb", "ast", "fga", "fta")
MINUTE_TOLERANCE_SEC = 90      # used only to break ties


def fp(row):
    return tuple(row.get(k) for k in FINGERPRINT)


def _mins(row):
    s = row.get("sec")
    return None if s is None else s / 60.0


def match_game(ours, theirs):
    """
    Return (mappings, ambiguous_count).

    Emit a mapping when the stat-line fingerprint is unique on both sides
    within a team. When it is not unique, fall back to minutes: pair each
    candidate with its closest counterpart, accepting only when that pairing
    is unambiguous and inside the tolerance.
    """
    mappings = []
    ambiguous = 0

    for team in {p["team"] for p in ours["players"]}:
        a = [p for p in ours["players"] if p["team"] == team]
        b = [p for p in theirs["players"] if p["team"] == team]
        if not a or not b:
            continue

        a_idx, b_idx = {}, {}
        for p in a:
            a_idx.setdefault(fp(p), []).append(p)
        for p in b:
            b_idx.setdefault(fp(p), []).append(p)

        for key, alist in a_idx.items():
            blist = b_idx.get(key, [])

            if len(alist) == 1 and len(blist) == 1:
                mappings.append((alist[0]["player_id"], blist[0]["nba_id"],
                                 blist[0]["name"]))
                continue

            if not blist:
                ambiguous += len(alist)
                continue

            # tie group — disambiguate on minutes
            resolved = 0
            for ap in alist:
                am = _mins(ap)
                if am is None:
                    continue
                scored = []
                for bp in blist:
                    bm = _mins(bp)
                    if bm is None:
                        continue
                    scored.append((abs(am - bm) * 60.0, bp))
                if not scored:
                    continue
                scored.sort(key=lambda x: x[0])
                best_d, best_p = scored[0]
                second_d = scored[1][0] if len(scored) > 1 else None
                if best_d <= MINUTE_TOLERANCE_SEC and (
                        second_d is None or second_d > best_d + 30):
                    mappings.append((ap["player_id"], best_p["nba_id"],
                                     best_p["name"]))
                    resolved += 1
            ambiguous += len(alist) - resolved

    return mappings, ambiguous


def cmd_diagnose(season):
    """Print one paired game side by side, and list unpaired games."""
    con = db()
    ours_all = our_rows_by_game(con, season)
    theirs_all = {}
    for st in ("Regular Season", "Playoffs"):
        theirs_all.update(nba_rows_by_game(fetch_gamelog(season, st)))

    unpaired = [k for k in ours_all if k not in theirs_all]
    print(f"\\n  local games {len(ours_all):,} | nba games {len(theirs_all):,} "
          f"| unpaired {len(unpaired)}")
    if unpaired:
        print("  unpaired (date, teams):")
        for k in sorted(unpaired)[:15]:
            print(f"    {k[0]}  {sorted(k[1])}")

    key = next((k for k in ours_all if k in theirs_all), None)
    if not key:
        print("  no paired game to inspect")
        con.close()
        return

    ours, theirs = ours_all[key], theirs_all[key]
    team = sorted({p["team"] for p in ours["players"]})[0]
    print(f"\\n  sample game {key[0]} {sorted(key[1])} — team {team}")
    print(f"  {'OURS (bbref)':38} | {'THEIRS (nba.com)'}")
    print(f"  {'sec':>6} {'min':>6} {'pts':>4}{'trb':>4}{'ast':>4}{'fga':>4}{'fta':>4}"
          f"   | {'sec':>6} {'min':>6} {'pts':>4}{'trb':>4}{'ast':>4}{'fga':>4}{'fta':>4}  name")
    a = sorted([p for p in ours["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    b = sorted([p for p in theirs["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    for i in range(max(len(a), len(b))):
        l = a[i] if i < len(a) else {}
        r = b[i] if i < len(b) else {}
        ls = l.get("sec"); rs = r.get("sec")
        print(f"  {str(ls):>6} {(ls/60 if ls else 0):>6.2f} "
              f"{str(l.get('pts')):>4}{str(l.get('trb')):>4}{str(l.get('ast')):>4}"
              f"{str(l.get('fga')):>4}{str(l.get('fta')):>4}   | "
              f"{str(rs):>6} {(rs/60 if rs else 0):>6.2f} "
              f"{str(r.get('pts')):>4}{str(r.get('trb')):>4}{str(r.get('ast')):>4}"
              f"{str(r.get('fga')):>4}{str(r.get('fta')):>4}  {r.get('name','')}")

    exact = sum(1 for p in b if p.get("sec") is not None and p["sec"] % 60 == 0)
    tot = sum(1 for p in b if p.get("sec") is not None)
    print(f"\\n  nba.com rows on whole minutes: {exact}/{tot}"
          f"  -> {'ROUNDED (confirms diagnosis)' if tot and exact == tot else 'exact seconds'}")
    con.close()
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"{TARGET} not found")
    src = open(TARGET, encoding="utf-8").read()

    if "MINUTE_TOLERANCE_SEC" in src:
        print("  already patched")
        return

    start = src.find("# Fields that form the fingerprint")
    if start == -1:
        start = src.find("FINGERPRINT = (")
    if start == -1:
        sys.exit("  could not locate FINGERPRINT definition")

    # everything from FINGERPRINT through end of match_game gets replaced
    end = src.find("def resolve(season, con", start)
    if end == -1:
        sys.exit("  could not locate resolve()")

    head = src[:start]
    tail = src[end:]

    # FINGERPRINT/fp/match_game live in two places in the file; keep only the
    # definitions block before resolve(), drop the stale duplicate if present.
    dup = head.find("def fp(row):")
    if dup != -1:
        cut = head.find("def match_game(ours, theirs):", dup)
        if cut != -1:
            nxt = head.find("\ndef ", cut + 10)
            if nxt != -1:
                head = head[:dup] + head[nxt + 1:]

    patched = head + NEW_BLOCK + "\n\n" + tail

    # wire the diagnose subcommand
    patched = patched.replace(
        'ap.add_argument("cmd", choices=["build", "update", "status"])',
        'ap.add_argument("cmd", choices=["build", "update", "status", "diagnose"])')
    patched = patched.replace(
        '    else:\n        cmd_status()',
        '    elif a.cmd == "diagnose":\n        cmd_diagnose(a.season)\n'
        '    else:\n        cmd_status()')

    import ast
    try:
        ast.parse(patched)
    except SyntaxError as e:
        sys.exit(f"  patch would produce invalid syntax ({e}) — nothing changed")

    open(TARGET + ".bak_fp", "w", encoding="utf-8").write(src)
    open(TARGET, "w", encoding="utf-8").write(patched)
    print("  fingerprint changed to (pts, trb, ast, fga, fta)")
    print("  minutes demoted to tiebreak with 90s tolerance")
    print("  added 'diagnose' subcommand")
    print("  backup -> bridge_nba_ids.py.bak_fp")


if __name__ == "__main__":
    main()
