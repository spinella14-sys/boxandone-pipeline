#!/usr/bin/env python3
"""
check_preseason.py — read-only. Did 2026-27 preseason make it into the
database, and into the files the Scoreboard and game pages read from R2?

    python3 check_preseason.py              # 2026-27
    python3 check_preseason.py 2025-26

Writes nothing, locally or to R2.
"""
import os
import sys
import tempfile

import duckdb

SEASON = sys.argv[1] if len(sys.argv) > 1 else "2026-27"
HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")


def load_env():
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def rows(con, sql):
    # one result set per connection — fetch fully before the next query
    return con.execute(sql).fetchall()


print("\n== hot database: %s ==" % DB_PATH)
con = duckdb.connect(DB_PATH, read_only=True)
print("  games by season_type for %s:" % SEASON)
for st, n, lo, hi in rows(con, """
        SELECT season_type, COUNT(*), MIN(game_date), MAX(game_date)
        FROM games WHERE season = '%s' GROUP BY 1 ORDER BY 1""" % SEASON):
    print("    %-14s %4d games  %s → %s" % (st, n, lo, hi))
print("  box-score rows by season_type:")
for st, games, n in rows(con, """
        SELECT g.season_type, COUNT(DISTINCT b.game_id), COUNT(*)
        FROM player_game_box b JOIN games g ON g.game_id = b.game_id
        WHERE g.season = '%s' GROUP BY 1 ORDER BY 1""" % SEASON):
    print("    %-14s %4d games with box  %6d rows" % (st, games, n))
con.close()

print("\n== R2: v2/seasons/%s/ ==" % SEASON)
import boto3
env = load_env()
s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                  aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                  region_name="auto")
bucket = env["R2_BUCKET_NAME"]
with tempfile.TemporaryDirectory() as tmp:
    for name in ("games.parquet", "player_game.parquet"):
        key = "v2/seasons/%s/%s" % (SEASON, name)
        local = os.path.join(tmp, name)
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            s3.download_file(bucket, key, local)
        except Exception as e:
            print("  %-20s MISSING (%s)" % (name, type(e).__name__))
            continue
        print("  %-20s last written %s" % (name, head["LastModified"]))
        c = duckdb.connect()
        cols = [r[0] for r in rows(c, "DESCRIBE SELECT * FROM read_parquet('%s')" % local)]
        if "season_type" in cols:
            for st, n in rows(c, """SELECT season_type, COUNT(*) FROM read_parquet('%s')
                                    GROUP BY 1 ORDER BY 1""" % local):
                print("    %-14s %6d rows" % (st, n))
        else:
            print("    no season_type column; columns: %s" % ", ".join(cols))
        c.close()
print()
