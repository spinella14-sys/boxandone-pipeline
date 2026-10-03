#!/usr/bin/env python3
"""
patch_bridge_seasontypes.py

7 games never pair with NBA.com: the NBA Cup final (2025-12-16) and the six
play-in games. The bridge only requests 'Regular Season' and 'Playoffs' from
leaguegamelog, and NBA.com files these under other season types.

I don't know the exact strings NBA.com uses, so this probes candidates against
the live endpoint, reports which return data, and writes the working ones into
bridge_nba_ids.py.

Run:  python3 patch_bridge_seasontypes.py
      python3 patch_bridge_seasontypes.py --probe-only
"""

import argparse
import ast
import os
import sys
import time

TARGET = os.path.expanduser("~/boxandone/bridge_nba_ids.py")
SEASON = "2025-26"

CANDIDATES = [
    "PlayIn", "Play In", "Play-In", "PlayIn Tournament",
    "IST", "In-Season Tournament", "Showcase",
    "All Star", "All-Star", "Pre Season",
]


def probe():
    from nba_api.stats.endpoints import leaguegamelog

    working = []
    for st in CANDIDATES:
        try:
            lg = leaguegamelog.LeagueGameLog(
                season=SEASON, season_type_all_star=st,
                player_or_team_abbreviation="P", timeout=30)
            rows = lg.get_normalized_dict()["LeagueGameLog"]
            games = len({r.get("GAME_ID") for r in rows})
            if rows:
                print(f"  {st:24} OK   {len(rows):>6,} rows  {games:>3} games")
                working.append(st)
            else:
                print(f"  {st:24} --   empty")
        except Exception as e:
            msg = str(e).split("\n")[0][:60]
            print(f"  {st:24} err  {msg}")
        time.sleep(2)
    return working


def apply(working):
    if not working:
        print("\n  no additional season types returned data — nothing to add")
        return

    src = open(TARGET, encoding="utf-8").read()
    old = 'def resolve(season, con, seasons_types=("Regular Season", "Playoffs")):'
    if old not in src:
        sys.exit("  resolve() signature not found — file differs from expected")

    types = ["Regular Season", "Playoffs"] + working
    lit = ", ".join(f'"{t}"' for t in types)
    new = f"def resolve(season, con, seasons_types=({lit})):"
    src2 = src.replace(old, new)

    # cmd_diagnose has its own hardcoded pair
    src2 = src2.replace(
        'for st in ("Regular Season", "Playoffs"):',
        f'for st in ({lit}):')

    try:
        ast.parse(src2)
    except SyntaxError as e:
        sys.exit(f"  invalid syntax ({e}) — nothing changed")

    open(TARGET + ".bak_st", "w", encoding="utf-8").write(src)
    open(TARGET, "w", encoding="utf-8").write(src2)
    print(f"\n  season types now: {types}")
    print("  backup -> bridge_nba_ids.py.bak_st")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-only", action="store_true")
    a = ap.parse_args()

    print(f"  probing leaguegamelog season types for {SEASON}\n")
    w = probe()
    print(f"\n  returned data: {w or 'none'}")

    if not a.probe_only:
        apply(w)
        print("\n  next: python3 bridge_nba_ids.py build --season 2025-26")
