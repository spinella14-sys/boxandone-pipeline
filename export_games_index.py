#!/usr/bin/env python3
"""
export_games_index.py — one small file per season listing its games.

The game selector reads the league schedule, which only covers the current
season. Anything older has to come from our own data, and there was no
game-grain export: player_game.parquet is per player, and team_season is per
team. So a scout could not pick a game from 2019 to write about.

Each file is about 40 KB — game id, date, both teams, final score, season type
— which is everything the selector shows and nothing it does not.

    python3 export_games_index.py check
    python3 export_games_index.py build
    python3 export_games_index.py build --season 2019-20

Writes v2/seasons/{season}/games.parquet.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")

SQL = """
SELECT g.game_id,
       g.season,
       g.season_type,
       g.game_date,
       g.home_abbr,
       g.away_abbr,
       g.home_score,
       g.away_score
FROM ag g
WHERE g.season = '%s'
ORDER BY g.game_date, g.home_abbr
"""


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def connect():
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    if os.path.isdir(CACHE) and os.listdir(CACHE):
        con.execute("""CREATE VIEW ag AS
            SELECT * FROM read_parquet('%s/*/games.parquet', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.games""" % CACHE)
    else:
        print("  no archive cache — current season only")
        con.execute("CREATE VIEW ag AS SELECT * FROM hot.games")
    return con


def seasons(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM ag ORDER BY season DESC").fetchall()]


def cmd_check(season):
    con = connect()
    target = season or seasons(con)[0]
    rows = con.execute(SQL % target).fetchall()
    cols = [d[0] for d in con.execute(SQL % target).description]
    print("  %s: %d games" % (target, len(rows)))
    print("  columns: %s" % cols)
    print()
    for r in rows[:4]:
        d = dict(zip(cols, r))
        print("    %s  %-4s %3s - %-3s %-4s  %s"
              % (d["game_date"], d["away_abbr"], d["away_score"],
                 d["home_score"], d["home_abbr"], d["season_type"]))
    types = {}
    for r in rows:
        d = dict(zip(cols, r))
        types[d["season_type"]] = types.get(d["season_type"], 0) + 1
    print("\n  by type: %s" % types)
    con.close()


def cmd_build(env, season):
    con = connect()
    s3 = s3c(env)
    targets = [season] if season else seasons(con)
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in targets:
            p = os.path.join(tmp, "g.parquet")
            if os.path.exists(p):
                os.remove(p)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (SQL % s, p))
            n = con.execute("SELECT COUNT(*) FROM ag WHERE season=?", [s]).fetchone()[0]
            if not n:
                continue
            s3.upload_file(p, env["R2_BUCKET_NAME"],
                           "v2/seasons/%s/games.parquet" % s)
            total += n
            print("  %-9s %5d games  %4.0f KB" % (s, n, os.path.getsize(p) / 1024))
    con.close()
    print("\n  %d games across %d seasons" % (total, len(targets)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season)
    else:
        cmd_build(load_env(), a.season)
