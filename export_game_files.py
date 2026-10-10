#!/usr/bin/env python3
"""
export_game_files.py — one play-by-play file per game, for the game page.

Writes v2/games/{game_id}/pbp.parquet: every event in the game in order —
clock, team, player, description, running score — with shot result, value,
distance and court coordinates on the shot rows. The game page draws both the
play-by-play and the shot chart from this one file, so a game costs one fetch
of roughly 15-25 KB rather than the season's whole play-by-play.

    python3 export_game_files.py check --season 2025-26     # count, write nothing
    python3 export_game_files.py build --season 2025-26
    python3 export_game_files.py build --all                # every season, ~38,000 files
    python3 export_game_files.py build --games ID,ID,...    # what the nightly job uses

Archived seasons are read from the local cache (export_full.py sync); without
it only the hot database is visible, which is what the nightly job has and all
it needs. Re-running is safe: each file is simply replaced.
"""
import argparse
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"

COLS = """action_id, event_group, period, clock_raw, seconds_left, elapsed_seconds,
          player_id, player_name, team_abbr, action_type, sub_type, description,
          shot_result, shot_value, shot_distance, x_legacy, y_legacy,
          is_field_goal, score_home, score_away"""


def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"):
        env.setdefault(k, os.environ.get(k, ""))
    return env


def s3c(env):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto",
                        config=Config(max_pool_connections=32,
                                      retries={"max_attempts": 5}))


def connect():
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    if os.path.isdir(CACHE) and os.listdir(CACHE):
        g = os.path.join(CACHE, "*", "%s")
        con.execute("""CREATE VIEW all_games AS
            SELECT * FROM read_parquet('%s', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.games""" % (g % "games.parquet"))
        con.execute("""CREATE VIEW all_pbp AS
            SELECT * FROM read_parquet('%s', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.play_by_play""" % (g % "play_by_play.parquet"))
    else:
        print("  no archive cache — current season only")
        con.execute("CREATE VIEW all_games AS SELECT * FROM hot.games")
        con.execute("CREATE VIEW all_pbp AS SELECT * FROM hot.play_by_play")
    return con


def write_games(con, s3, bucket, where, label):
    """Partition the matching events by game and upload one file per game."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "parts")
        con.execute("""
            COPY (SELECT game_id, %s FROM all_pbp WHERE %s ORDER BY game_id, action_id)
            TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (game_id))"""
                    % (COLS, where, out))
        jobs = []
        if os.path.isdir(out):
            for d in os.listdir(out):
                if not d.startswith("game_id="):
                    continue
                gid = d.split("=", 1)[1]
                files = [f for f in os.listdir(os.path.join(out, d)) if f.endswith(".parquet")]
                if files:
                    jobs.append((gid, os.path.join(out, d, files[0])))

        def up(job):
            gid, path = job
            s3.upload_file(path, bucket, "%s/games/%s/pbp.parquet" % (PREFIX, gid),
                           ExtraArgs={"ContentType": "application/octet-stream"})
            return os.path.getsize(path)

        total = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            for n in pool.map(up, jobs):
                total += n
        print("  %-12s %5d games  %6.1f MB" % (label, len(jobs), total / 1048576.0))
        return len(jobs)


def seasons(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM all_games ORDER BY season DESC").fetchall()]


def season_where(s):
    return "game_id IN (SELECT game_id FROM all_games WHERE season = '%s')" % s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--games", help="comma-separated game ids")
    a = ap.parse_args()
    con = connect()

    if a.games:
        ids = [g.strip() for g in a.games.split(",") if g.strip()]
        targets = [("games", "game_id IN (%s)" % ",".join("'%s'" % g.replace("'", "") for g in ids))]
    elif a.all:
        targets = [(s, season_where(s)) for s in seasons(con)]
    elif a.season:
        targets = [(a.season, season_where(a.season))]
    else:
        sys.exit("  give --season, --all or --games")

    if a.cmd == "check":
        for label, where in targets:
            g, e = con.execute("SELECT COUNT(DISTINCT game_id), COUNT(*) FROM all_pbp WHERE %s"
                               % where).fetchone()
            print("  %-12s %5d games  %8d events" % (label, g, e))
        return

    env = load_env()
    s3 = s3c(env)
    total = 0
    for label, where in targets:
        total += write_games(con, s3, env["R2_BUCKET_NAME"], where, label)
    print("\n  %d game files written" % total)


if __name__ == "__main__":
    main()
