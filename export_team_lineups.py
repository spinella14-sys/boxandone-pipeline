#!/usr/bin/env python3
"""
export_team_lineups.py — every 5-, 4-, 3- and 2-man combination a team used.

  v2/teams/{abbr}/lineups/{season}.parquet

Built from the season's stints (build_lineups.py, data/lineups/{season}.parquet):
each stint is one stretch with the same ten players on the floor, carrying the
points, possessions and box tallies of both sides. A stint counts toward its
five-man unit and toward every 4-, 3- and 2-man group inside it, so a pair's
minutes are every minute those two shared the floor, whoever the other three
were.

One row per team, season type and combination:
  size            2-5
  player_ids      our ids, comma separated, sorted
  season_type     regular / playin / playoff
  games, seconds, stints
  pts_for, pts_against, poss_for, poss_against
  fgm fga fg3m fg3a ftm fta orb drb tov ast stl blk   and opp_ of each

Only games whose substitutions reconciled with the box score (reliable) are
counted, and a stint with a player we cannot bridge to our id is skipped, so
team totals here can sit a little under the box score's. Files exist from
2019-20, where the stints begin.

Team codes are NBA.com's (PHX, BKN, CHA), like the team shot files.

    python3 export_team_lineups.py build --season 2026-27            # the nightly
    python3 export_team_lineups.py build --seasons 2019-20:2026-27   # everything, once
    python3 export_team_lineups.py check --season 2025-26            # counts, writes nothing
"""
import argparse
import os
import sys
import tempfile
from collections import defaultdict
from itertools import combinations

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
LINEUPS = os.path.join(HOME, "data", "lineups")
PREFIX = "v2"
FIRST = "2019-20"

FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
SUMS = ["seconds", "stints", "pts_for", "pts_against", "poss_for", "poss_against"] + \
       list(FACTORS) + ["opp_" + k for k in FACTORS]
# NBA game ids carry the season type in their first three digits
TYPES = {"002": "regular", "004": "playoff", "005": "playin"}


def season_list(a):
    if a.season:
        return [a.season]
    lo, hi = a.seasons.split(":")
    y0, y1 = int(lo[:4]), int(hi[:4])
    return ["%d-%02d" % (y, (y + 1) % 100) for y in range(y0, y1 + 1)]


def stints_file(season, s3=None, bucket=None):
    path = os.path.join(LINEUPS, "%s.parquet" % season)
    if os.path.exists(path):
        return path
    if s3 is None:
        return None
    os.makedirs(LINEUPS, exist_ok=True)
    try:
        s3.download_file(bucket, "%s/lineups/%s.parquet" % (PREFIX, season), path)
        return path
    except Exception:
        return None


def aggregate(con, path, ids):
    have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % path).fetchall()}
    fcols = ", ".join(c if c in have else "NULL" for c in
                      ["%s_%s" % (s, k) for s in ("home", "away") for k in FACTORS])
    rows = con.execute("""
        SELECT game_id, home, away, seconds, home_ids, away_ids,
               home_pts, away_pts, home_poss, away_poss, %s
        FROM read_parquet('%s') WHERE reliable""" % (fcols, path)).fetchall()
    agg = {}
    games = defaultdict(set)
    skipped = 0
    n = len(FACTORS)
    for r in rows:
        gid, home, away, secs, hids, aids, hp, ap, hpo, apo = r[:10]
        fh, fa = r[10:10 + n], r[10 + n:10 + 2 * n]
        stype = TYPES.get(str(gid)[:3])
        if not stype:
            continue
        for team, mine, pf, pa, qf, qa, own, opp in (
                (home, hids, hp, ap, hpo, apo, fh, fa),
                (away, aids, ap, hp, apo, hpo, fa, fh)):
            nba = [int(x) for x in (mine or "").split(",") if x]
            ours = [ids.get(x) for x in nba]
            if len(ours) != 5 or not all(ours):
                skipped += 1
                continue
            ours.sort()
            vals = [secs or 0, 1, pf or 0, pa or 0, qf or 0, qa or 0] + \
                   [v or 0 for v in own] + [v or 0 for v in opp]
            for size in (5, 4, 3, 2):
                for combo in combinations(ours, size):
                    key = (team, stype, size, ",".join(combo))
                    u = agg.get(key)
                    if u is None:
                        agg[key] = list(vals)
                    else:
                        for i, v in enumerate(vals):
                            u[i] += v
                    games[key].add(gid)
    return agg, games, skipped, bool(fcols.replace("NULL", "").replace(",", "").strip())


def write(con, s3, bucket, season, agg, games, has_factors, dry):
    by_team = defaultdict(list)
    for (team, stype, size, pids), v in agg.items():
        row = {"size": size, "player_ids": pids, "season_type": stype, "games": len(games[(team, stype, size, pids)])}
        row.update(zip(SUMS, v))
        if not has_factors:
            for k in SUMS[6:]:
                row[k] = None
        by_team[team].append(row)
    if dry:
        for team in sorted(by_team):
            rs = by_team[team]
            print("    %s  %5d combinations  (%d five-man)" % (team, len(rs), sum(1 for r in rs if r["size"] == 5)))
        return len(by_team)
    cols = ["size", "player_ids", "season_type", "games"] + SUMS
    types = ["INTEGER", "VARCHAR", "VARCHAR", "INTEGER"] + ["DOUBLE"] * len(SUMS)
    with tempfile.TemporaryDirectory() as tmp:
        for team, rs in by_team.items():
            rs.sort(key=lambda r: (r["season_type"], r["size"], -r["seconds"]))
            con.execute("CREATE OR REPLACE TEMP TABLE tl (%s)"
                        % ", ".join('"%s" %s' % (c, t) for c, t in zip(cols, types)))
            con.executemany("INSERT INTO tl VALUES (%s)" % ",".join("?" * len(cols)),
                            [[r[c] for c in cols] for r in rs])
            p = os.path.join(tmp, "%s.parquet" % team)
            con.execute("COPY tl TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % p)
            s3.upload_file(p, bucket, "%s/teams/%s/lineups/%s.parquet" % (PREFIX, team, season),
                           ExtraArgs={"ContentType": "application/octet-stream"})
    return len(by_team)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "check"])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--season")
    g.add_argument("--seasons")
    a = ap.parse_args()

    import duckdb
    import nightly2 as N
    import export_game_lineups as GL
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    _, ids, _ = GL.maps()
    con = duckdb.connect()
    for season in season_list(a):
        if season < FIRST:
            print("  %s: stints begin with %s — skipped" % (season, FIRST))
            continue
        path = stints_file(season, s3, bucket)
        if not path:
            print("  %s: no stints on file — build_lineups.py build --seasons %s first" % (season, season))
            continue
        agg, games, skipped, has_f = aggregate(con, path, ids)
        teams = write(con, s3, bucket, season, agg, games, has_f, a.cmd == "check")
        print("  %s: %d teams, %d combinations%s%s" % (
            season, teams, len(agg),
            "" if has_f else ", score only (stints predate the box tallies)",
            (", %d team-stints skipped (a player without our id)" % skipped) if skipped else ""))


if __name__ == "__main__":
    main()
