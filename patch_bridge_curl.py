#!/usr/bin/env python3
"""
patch_bridge_curl.py

Two problems, both blocking the player bridge from running on older seasons.

1. bridge_nba_ids.py calls stats.nba.com through nba_api, which uses plain
   `requests`. stats.nba.com now fingerprints the TLS handshake and silently
   drops non-browser clients — verified: requests and curl both time out at
   30s while a browser loads fine. curl_cffi with Chrome impersonation works.

2. CACHE COLLISION. bridge_games.py and bridge_nba_ids.py both write
   ~/boxandone/raw/nba_gamelogs/{season}_{type}.json, but the game bridge
   stores TEAM-level rows (PlayerOrTeam=T, 2 rows/game) and the player bridge
   stores PLAYER-level rows (PlayerOrTeam=P, ~26 rows/game). The 2025-26 files
   are player-level from the original run; every other season is team-level
   from the game bridge. A patched player bridge reading those would find no
   PLAYER_ID and match nothing, with no error.

   Player-level caches now carry a _players suffix. Nothing is overwritten.

Also adds a season range so the whole history runs in one command:

    python3 bridge_nba_ids.py build --seasons 2024-25:1996-97

Run:  python3 patch_bridge_curl.py
"""

import ast
import os
import re
import sys

TARGET = os.path.expanduser("~/boxandone/bridge_nba_ids.py")

NEW_FETCH = '''def fetch_gamelog(season, season_type, force=False):
    """One request -> every player-game line for a season. Cached to disk.

    Uses curl_cffi: stats.nba.com fingerprints TLS and drops plain clients.
    Cache filename carries a _players suffix so it cannot collide with
    bridge_games.py's team-level cache for the same season and type.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    tag = season_type.replace(" ", "_")
    path = os.path.join(CACHE_DIR, "%s_%s_players.json" % (season, tag))

    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    from curl_cffi import requests as cr

    url = ("https://stats.nba.com/stats/leaguegamelog"
           "?Counter=0&Season=%s&SeasonType=%s&PlayerOrTeam=P"
           "&Direction=DESC&Sorter=DATE&LeagueID=00&DateFrom=&DateTo="
           % (season, season_type.replace(" ", "+")))
    headers = {
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
        "Accept": "application/json, text/plain, */*",
    }

    for attempt in range(4):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s %s: %s" % (season, season_type, str(e)[:60]))
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            print("      %s %s: HTTP %s" % (season, season_type, r.status_code))
            time.sleep(15 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except Exception:
            time.sleep(15)
            continue

        sets = payload.get("resultSets") or []
        if not sets or not sets[0].get("rowSet"):
            # season type does not exist for this season (e.g. PlayIn pre-2020)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []

        hdr = sets[0]["headers"]
        rows = [dict(zip(hdr, row)) for row in sets[0]["rowSet"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        print("    %s %s: %d player-game rows" % (season, season_type, len(rows)))
        time.sleep(2.5)
        return rows
    return []
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("%s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "curl_cffi" in src:
        print("  already patched")
        return

    start = src.find("def fetch_gamelog(")
    if start == -1:
        sys.exit("  fetch_gamelog not found")
    end = src.find("\ndef ", start + 10)
    if end == -1:
        sys.exit("  could not bound fetch_gamelog")

    src = src[:start] + NEW_FETCH + src[end:]

    # season range support
    src = src.replace(
        '    ap.add_argument("--season", default="2025-26")',
        '    ap.add_argument("--season", default="2025-26")\n'
        '    ap.add_argument("--seasons", default=None,\n'
        '                    help="range, newest first, e.g. 2024-25:1996-97")')
    src = src.replace(
        '    if a.cmd == "build":\n        cmd_build(a.season)',
        '    if a.cmd == "build":\n'
        '        if a.seasons:\n'
        '            hi, lo = a.seasons.split(":")\n'
        '            hi_y, lo_y = int(hi.split("-")[0]), int(lo.split("-")[0])\n'
        '            for y in range(hi_y, lo_y - 1, -1):\n'
        '                cmd_build("%d-%s" % (y, str(y + 1)[2:]))\n'
        '        else:\n'
        '            cmd_build(a.season)')

    if "import time" not in src:
        src = src.replace("import sys", "import sys\nimport time", 1)

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    for fn in ("db", "fetch_gamelog", "nba_rows_by_game", "our_rows_by_game",
               "match_game", "resolve", "write_ids", "cmd_build", "cmd_status"):
        if "def %s(" % fn not in src:
            sys.exit("  sanity check failed: %s missing — nothing changed" % fn)

    open(TARGET + ".bak_curl", "w", encoding="utf-8").write(src if False else
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)

    print("  fetch_gamelog now uses curl_cffi (chrome impersonation)")
    print("  player-level cache renamed with _players suffix (no collision)")
    print("  added --seasons range")
    print("  backup -> bridge_nba_ids.py.bak_curl")

    # report the collision state so it is visible rather than assumed
    cache = os.path.expanduser("~/boxandone/raw/nba_gamelogs")
    if os.path.isdir(cache):
        import json as _j
        team_level = player_level = 0
        for f in sorted(os.listdir(cache)):
            if not f.endswith(".json") or f.endswith("_players.json"):
                continue
            try:
                rows = _j.load(open(os.path.join(cache, f), encoding="utf-8"))
            except Exception:
                continue
            if rows and "PLAYER_ID" in rows[0]:
                player_level += 1
            elif rows:
                team_level += 1
        print("\n  existing cache: %d team-level files, %d player-level files"
              % (team_level, player_level))
        print("  (player-level ones are now ignored; new _players files will be fetched)")


if __name__ == "__main__":
    main()
