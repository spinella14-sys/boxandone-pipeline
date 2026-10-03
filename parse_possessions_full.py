#!/usr/bin/env python3
"""
parse_possessions_full.py — possessions for all thirty seasons.

The earlier parser read DuckDB, which after archiving holds only the current
season. This reads the union of the archive cache and the hot database, the
same way export_full.py does, and writes one Parquet per season to R2 rather
than growing the hot file back toward 400 MB.

Coverage was checked before building: across 1996-97 to 2025-26 events per game
sit between 468 and 506, substitutions between 36 and 53, and free-throw
sequence labels never fall below 96.2%. The parse is as reliable in 1997 as it
is now, so there is no era boundary to work around.

    python3 parse_possessions_full.py check --season 1996-97
    python3 parse_possessions_full.py build            # all seasons
    python3 parse_possessions_full.py build --season 2015-16
    python3 parse_possessions_full.py status

Writes v2/possessions/{season}.parquet — about 3 MB a season.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2/possessions"

sys.path.insert(0, HOME)
try:
    from parse_possessions import parse_game, clock_secs       # the proven parser
except ImportError:
    sys.exit("  parse_possessions.py must be in %s" % HOME)


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
    """In-memory connection with the hot database attached read-only, so the
    archive Parquet and the live season can be queried as one."""
    import duckdb
    if not os.path.isdir(CACHE) or not os.listdir(CACHE):
        sys.exit("  archive cache empty — run: python3 export_full.py sync")
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    con.execute("""CREATE VIEW ag AS
        SELECT * FROM read_parquet('%s/*/games.parquet', union_by_name=true)
        UNION ALL BY NAME SELECT * FROM hot.games""" % CACHE)
    con.execute("""CREATE VIEW ap AS
        SELECT * FROM read_parquet('%s/*/play_by_play.parquet', union_by_name=true)
        UNION ALL BY NAME SELECT * FROM hot.play_by_play""" % CACHE)
    return con


def seasons_in(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM ag ORDER BY season DESC").fetchall()]


def games_of(con, season):
    """Stream one season's events, grouped by game."""
    cur = con.execute("""
        SELECT e.game_id, e.action_id, e.period, e.clock_raw, e.action_type,
               e.sub_type, e.team_abbr, e.shot_result, e.shot_value, e.description,
               g.home_abbr, g.away_abbr
        FROM ap e JOIN ag g ON g.game_id = e.game_id
        WHERE g.season = ?
        ORDER BY e.game_id, e.action_id""", [season])
    cols = [d[0] for d in cur.description]
    game, rows, meta = None, [], None
    while True:
        batch = cur.fetchmany(100000)
        if not batch:
            break
        for r in batch:
            d = dict(zip(cols, r))
            d["secs"] = clock_secs(d.get("clock_raw"))
            if d["game_id"] != game:
                if rows:
                    yield game, rows, meta
                game, rows = d["game_id"], []
                meta = (d.get("home_abbr"), d.get("away_abbr"))
            rows.append(d)
    if rows:
        yield game, rows, meta


def parse_season(con, season):
    out = []
    games = 0
    for gid, events, meta in games_of(con, season):
        games += 1
        home, away = meta or (None, None)
        for r in parse_game(events):
            off = r["off_team"]
            deff = away if off == home else home
            out.append((gid, season, r["period"], r["poss_num"], off, deff,
                        r["start_action"], r["end_action"],
                        r["start_secs"], r["end_secs"], r["points"], r["end_reason"]))
    return games, out


def cmd_check(season):
    con = connect()
    target = season or seasons_in(con)[0]
    games, rows = parse_season(con, target)
    pts = sum(r[10] for r in rows)
    reasons = {}
    for r in rows:
        reasons[r[11]] = reasons.get(r[11], 0) + 1
    print("  %s" % target)
    print("    games        %d" % games)
    print("    possessions  %d  (%.1f per game, %.1f per team)"
          % (len(rows), len(rows) / games, len(rows) / games / 2))
    print("    points       %d  (%.3f per possession)" % (pts, pts / len(rows)))
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("    %-12s %7d  %5.1f%%" % (k, v, 100 * v / len(rows)))
    con.close()


def cmd_build(env, season):
    import duckdb
    con = connect()
    s3 = s3c(env)
    targets = [season] if season else seasons_in(con)
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in targets:
            games, rows = parse_season(con, s)
            if not rows:
                print("  %-9s no events" % s)
                continue
            w = duckdb.connect()
            w.execute("""CREATE TABLE p (game_id VARCHAR, season VARCHAR,
                period SMALLINT, poss_num INTEGER, off_team VARCHAR,
                def_team VARCHAR, start_action BIGINT, end_action BIGINT,
                start_secs DOUBLE, end_secs DOUBLE, points SMALLINT,
                end_reason VARCHAR)""")
            w.executemany("INSERT INTO p VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            path = os.path.join(tmp, "p.parquet")
            if os.path.exists(path):
                os.remove(path)
            w.execute("COPY p TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % path)
            w.close()
            s3.upload_file(path, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, s))
            pts = sum(r[10] for r in rows)
            total += len(rows)
            print("  %-9s %5d games  %7d poss  %5.1f/g  %.3f ppp  %4.0f KB"
                  % (s, games, len(rows), len(rows) / games, pts / len(rows),
                     os.path.getsize(path) / 1024))
    con.close()
    print("\n  %d possessions across %d seasons" % (total, len(targets)))


def cmd_status(env):
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    keys, token, size = [], None, 0
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            keys.append(o["Key"])
            size += o["Size"]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if not keys:
        print("  nothing built yet")
        return
    print("  %d seasons, %.1f MB" % (len(keys), size / 1048576.0))
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for k in sorted(keys, reverse=True)[:5]:
            p = os.path.join(tmp, "x.parquet")
            s3.download_file(bucket, k, p)
            r = con.execute("""SELECT COUNT(*), COUNT(DISTINCT game_id),
                SUM(points)::DOUBLE/COUNT(*) FROM read_parquet('%s')""" % p).fetchone()
            print("    %-28s %7d poss  %5d games  %.3f ppp"
                  % (k.split("/")[-1], r[0], r[1], r[2]))
            os.remove(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "status"])
    ap.add_argument("--season")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season)
    elif a.cmd == "build":
        cmd_build(load_env(), a.season)
    else:
        cmd_status(load_env())
