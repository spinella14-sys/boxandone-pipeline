#!/usr/bin/env python3
"""
bridge_games.py — map our game_id to NBA.com's GAME_ID.

The player bridge paired games by date + teams and then threw the pairings
away, keeping only player identifiers. PBP, shot detail and tracking all key
off NBA GAME_IDs, so the mapping has to be stored.

Cheap: leaguegamelog returns every game for a season in ONE request, so the
whole 1997-2026 mapping is ~150 requests rather than 38,021.

    python3 bridge_games.py build --seasons 2025-26:1996-97
    python3 bridge_games.py status

Uses curl_cffi with Chrome TLS impersonation. stats.nba.com fingerprints the
TLS handshake and silently drops plain requests/curl clients — verified: both
time out at 30s while a browser loads fine.

Requires: curl_cffi
"""

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "raw", "nba_gamelogs")

DELAY = 2.5
IMPERSONATE = "chrome"

HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}

SEASON_TYPES = ["Regular Season", "Playoffs", "PlayIn", "IST"]

TEAM_ALIASES = {
    "BRK": "BKN", "BKN": "BKN", "NJN": "BKN",
    "PHO": "PHX", "PHX": "PHX",
    "CHO": "CHA", "CHA": "CHA", "CHH": "CHA",
    "NOH": "NOP", "NOK": "NOP", "NOP": "NOP",
    "SEA": "SEA", "OKC": "OKC",
    "VAN": "VAN", "MEM": "MEM",
    "WSB": "WAS", "WAS": "WAS",
    "SDC": "LAC", "LAC": "LAC",
    "KCK": "SAC", "SAC": "SAC",
}


def norm_team(a):
    if not a:
        return None
    a = a.strip().upper()
    return TEAM_ALIASES.get(a, a)


def db():
    import duckdb
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS game_identifiers (
            source          VARCHAR NOT NULL,
            source_game_id  VARCHAR NOT NULL,
            game_id         VARCHAR NOT NULL,
            season          VARCHAR,
            season_type     VARCHAR,
            game_date       DATE,
            linked_by       VARCHAR,
            linked_at       TIMESTAMP DEFAULT now(),
            PRIMARY KEY (source, source_game_id)
        )
    """)
    return con


def fetch_log(season, season_type, force=False):
    """One request -> every player-game row for a season+type. Cached."""
    os.makedirs(CACHE, exist_ok=True)
    tag = season_type.replace(" ", "_")
    path = os.path.join(CACHE, "%s_%s.json" % (season, tag))

    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    from curl_cffi import requests as cr
    url = ("https://stats.nba.com/stats/leaguegamelog"
           "?Counter=0&Season=%s&SeasonType=%s&PlayerOrTeam=T"
           "&Direction=DESC&Sorter=DATE&LeagueID=00&DateFrom=&DateTo="
           % (season, season_type.replace(" ", "+")))

    for attempt in range(4):
        try:
            r = cr.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=60)
        except Exception as e:
            print("      network: %s" % str(e)[:70])
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            print("      HTTP %s" % r.status_code)
            time.sleep(15 * (attempt + 1))
            continue
        try:
            d = r.json()
        except Exception:
            print("      unparseable response")
            time.sleep(15)
            continue

        rs = d.get("resultSets") or []
        if not rs or not rs[0].get("rowSet"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        hdr = rs[0]["headers"]
        rows = [dict(zip(hdr, row)) for row in rs[0]["rowSet"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        return rows
    return []


def index_nba(rows):
    """{(date, frozenset(teams)): game_id} from TEAM-level rows (2 per game)."""
    by_gid = defaultdict(set)
    dates = {}
    for r in rows:
        gid = r.get("GAME_ID")
        if not gid:
            continue
        by_gid[gid].add(norm_team(r.get("TEAM_ABBREVIATION")))
        dates[gid] = (r.get("GAME_DATE") or "")[:10]
    out = {}
    for gid, teams in by_gid.items():
        teams.discard(None)
        if len(teams) == 2 and dates.get(gid):
            out[(dates[gid], frozenset(teams))] = gid
    return out


def index_ours(con, season):
    rows = con.execute("""
        SELECT game_id, CAST(game_date AS VARCHAR), home_abbr, away_abbr, season_type
        FROM games WHERE season = ?
    """, [season]).fetchall()
    out = {}
    for gid, d, home, away, st in rows:
        teams = frozenset([norm_team(home), norm_team(away)])
        if len(teams) == 2:
            out[(d, teams)] = (gid, st)
    return out


def cmd_build(seasons, force=False):
    con = db()
    grand_ok = grand_miss = 0

    for season in seasons:
        ours = index_ours(con, season)
        if not ours:
            print("  %s: no local games, skipped" % season)
            continue

        theirs = {}
        per_type = {}
        for st in SEASON_TYPES:
            rows = fetch_log(season, st, force=force)
            idx = index_nba(rows)
            for k, v in idx.items():
                theirs[k] = v
                per_type[v] = st
            if rows:
                time.sleep(DELAY + random.uniform(0, 1))

        ok = miss = 0
        for key, (our_gid, our_st) in ours.items():
            nba_gid = theirs.get(key)
            if not nba_gid:
                miss += 1
                continue
            con.execute("""
                INSERT INTO game_identifiers
                  (source, source_game_id, game_id, season, season_type, game_date, linked_by)
                VALUES ('nba', ?, ?, ?, ?, ?, 'bridge_games')
                ON CONFLICT (source, source_game_id) DO NOTHING
            """, [nba_gid, our_gid, season, per_type.get(nba_gid), key[0]])
            ok += 1

        con.commit()
        flag = "" if miss == 0 else "   <- %d unmatched" % miss
        print("  %-9s ours %4d | nba %4d | mapped %4d%s"
              % (season, len(ours), len(theirs), ok, flag))
        grand_ok += ok
        grand_miss += miss

    print("\n  mapped %d, unmatched %d" % (grand_ok, grand_miss))
    con.close()


def cmd_status():
    con = db()
    rows = con.execute("""
        SELECT g.season,
               COUNT(*) AS games,
               COUNT(gi.game_id) AS mapped
        FROM games g
        LEFT JOIN game_identifiers gi
               ON gi.game_id = g.game_id AND gi.source='nba'
        GROUP BY g.season ORDER BY g.season DESC
    """).fetchall()
    print("  %-9s %7s %7s %7s" % ("season", "games", "mapped", "gap"))
    for s, n, m in rows:
        print("  %-9s %7d %7d %7d" % (s, n, m, n - m))
    tot = con.execute("SELECT COUNT(*) FROM game_identifiers WHERE source='nba'").fetchone()[0]
    print("\n  total nba game mappings: %d" % tot)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "status"])
    ap.add_argument("--seasons", default="2025-26:1996-97")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.cmd == "status":
        cmd_status()
    else:
        hi, lo = a.seasons.split(":")
        hi_y, lo_y = int(hi.split("-")[0]), int(lo.split("-")[0])
        seasons = ["%d-%s" % (y, str(y + 1)[2:]) for y in range(hi_y, lo_y - 1, -1)]
        cmd_build(seasons, force=a.force)
