#!/usr/bin/env python3
"""
export_r2.py — publish DuckDB data to R2 as Parquet.

Layout (everything under v2/ so the old project's objects are untouched):

  v2/registry/players.parquet              every player + current position
  v2/seasons/{season}/player_season.parquet  all players' totals for a season
  v2/seasons/{season}/team_season.parquet    team totals (pace, possessions)
  v2/players/{player_id}/gamelog.parquet     one player's whole career, game grain
  v2/players/{player_id}/shots.parquet       one player's whole career, shot grain
  v2/manifest.json                           what exists and when it was written

The split follows how screens actually read:
  - the Stat Database wants every player in ONE season -> one file per season
  - a player page wants ONE player across every season -> one file per player

Both are small. A season aggregate is ~550 rows; a career game log is ~1,000.

  python3 export_r2.py seasons              # 30 season files
  python3 export_r2.py players              # per-player files
  python3 export_r2.py registry
  python3 export_r2.py all
  python3 export_r2.py status               # compare R2 against the database
  python3 export_r2.py verify --sample 20   # download and row-count check

Credentials from ~/boxandone/.env. Requires boto3, duckdb.
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"


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


def s3_client(env):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT_URL"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(max_pool_connections=20, retries={"max_attempts": 3}),
    )


def db():
    import duckdb
    return duckdb.connect(DB_PATH, read_only=True)


def put_parquet(con, s3, bucket, query, key, tmpdir):
    """Run query -> local parquet -> R2. Returns (rows, bytes) or None if empty."""
    path = os.path.join(tmpdir, "out.parquet")
    if os.path.exists(path):
        os.remove(path)
    con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (query, path))
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        body = f.read()
    if len(body) < 100:          # parquet header only == no rows
        return None
    s3.put_object(Bucket=bucket, Key=key, Body=body,
                  ContentType="application/octet-stream")
    return size


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

SEASON_SQL = """
SELECT
    b.player_id,
    p.full_name,
    g.season,
    g.league,
    g.season_type,
    COUNT(DISTINCT b.team_abbr)                          AS teams,
    MAX(b.team_abbr)                                     AS last_team,
    COUNT(*)                                             AS gp,
    SUM(CASE WHEN b.started THEN 1 ELSE 0 END)           AS gs,
    SUM(b.seconds_played) / 60.0                         AS mp,
    SUM(b.fgm) AS fgm, SUM(b.fga) AS fga,
    SUM(b.fg3m) AS fg3m, SUM(b.fg3a) AS fg3a,
    SUM(b.ftm) AS ftm, SUM(b.fta) AS fta,
    SUM(b.orb) AS orb, SUM(b.drb) AS drb, SUM(b.trb) AS trb,
    SUM(b.ast) AS ast, SUM(b.stl) AS stl, SUM(b.blk) AS blk,
    SUM(b.tov) AS tov, SUM(b.pf) AS pf, SUM(b.pts) AS pts,
    SUM(b.plus_minus)                                    AS plus_minus,
    AVG(date_diff('day', p.birthdate, g.game_date) / 365.25) AS age,
    MIN(g.game_date) AS first_game,
    MAX(g.game_date) AS last_game
FROM player_game_box b
JOIN games   g ON g.game_id   = b.game_id
JOIN players p ON p.player_id = b.player_id
WHERE b.played AND g.season = '%s'
GROUP BY 1,2,3,4,5
"""

TEAM_SEASON_SQL = """
SELECT
    g.season, g.league, g.season_type, b.team_abbr,
    COUNT(DISTINCT g.game_id)                  AS gp,
    SUM(b.fga) AS fga, SUM(b.fta) AS fta,
    SUM(b.orb) AS orb, SUM(b.tov) AS tov,
    SUM(b.fgm) AS fgm, SUM(b.fg3m) AS fg3m, SUM(b.ftm) AS ftm,
    SUM(b.pts) AS pts, SUM(b.trb) AS trb, SUM(b.ast) AS ast,
    -- standard possession estimate; enables per-100 on the client
    SUM(b.fga) + 0.44 * SUM(b.fta) - SUM(b.orb) + SUM(b.tov) AS poss_est
FROM player_game_box b
JOIN games g ON g.game_id = b.game_id
WHERE b.played AND g.season = '%s'
GROUP BY 1,2,3,4
"""

GAMELOG_SQL = """
SELECT
    b.game_id, g.game_date, g.season, g.league, g.season_type,
    b.team_abbr, b.opp_abbr, b.is_home, b.started,
    b.seconds_played / 60.0 AS mp,
    b.fgm, b.fga, b.fg3m, b.fg3a, b.ftm, b.fta,
    b.orb, b.drb, b.trb, b.ast, b.stl, b.blk, b.tov, b.pf, b.pts,
    b.plus_minus,
    g.home_score, g.away_score,
    date_diff('day', p.birthdate, g.game_date) / 365.25 AS age
FROM player_game_box b
JOIN games   g ON g.game_id = b.game_id
JOIN players p ON p.player_id = b.player_id
WHERE b.player_id = '%s' AND b.played
ORDER BY g.game_date
"""

SHOTS_SQL = """
SELECT
    e.game_id, g.game_date, g.season, g.season_type,
    e.period, e.seconds_left, e.elapsed_seconds,
    e.team_abbr, e.shot_result, e.shot_value, e.shot_distance,
    e.x_legacy, e.y_legacy, e.sub_type AS shot_type, e.description
FROM play_by_play e
JOIN games g ON g.game_id = e.game_id
WHERE e.player_id = '%s' AND e.is_field_goal
ORDER BY g.game_date, e.action_id
"""

REGISTRY_SQL = """
SELECT
    p.player_id, p.full_name, p.display_name, p.name_normalized,
    p.birthdate, p.birthdate_status, p.position,
    p.height_in, p.weight_lb, p.college, p.draft_year, p.draft_round,
    p.draft_pick, p.status, p.nationality,
    i.source_id AS bbref_id,
    n.source_id AS nba_id,
    s.seasons, s.first_season, s.last_season, s.career_gp, s.career_pts
FROM players p
LEFT JOIN player_identifiers i ON i.player_id=p.player_id AND i.source='bbref'
LEFT JOIN player_identifiers n ON n.player_id=p.player_id AND n.source='nba'
LEFT JOIN (
    SELECT b.player_id,
           COUNT(DISTINCT g.season) AS seasons,
           MIN(g.season) AS first_season,
           MAX(g.season) AS last_season,
           COUNT(*) AS career_gp,
           SUM(b.pts) AS career_pts
    FROM player_game_box b JOIN games g ON g.game_id=b.game_id
    WHERE b.played GROUP BY 1
) s ON s.player_id = p.player_id
"""


# ---------------------------------------------------------------------------

def cmd_seasons(env, only=None):
    con, s3 = db(), s3_client(env)
    bucket = env["R2_BUCKET_NAME"]
    seasons = [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM games ORDER BY season DESC").fetchall()]
    if only:
        seasons = [s for s in seasons if s == only]

    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            k1 = "%s/seasons/%s/player_season.parquet" % (PREFIX, s)
            k2 = "%s/seasons/%s/team_season.parquet" % (PREFIX, s)
            n1 = put_parquet(con, s3, bucket, SEASON_SQL % s, k1, tmp)
            n2 = put_parquet(con, s3, bucket, TEAM_SEASON_SQL % s, k2, tmp)
            total += (n1 or 0) + (n2 or 0)
            print("  %-9s player %6.1f KB   team %5.1f KB"
                  % (s, (n1 or 0) / 1024.0, (n2 or 0) / 1024.0))
    print("\n  %d seasons, %.1f MB" % (len(seasons), total / 1048576.0))
    con.close()


def cmd_players(env, limit=None, skip_existing=False):
    con, s3 = db(), s3_client(env)
    bucket = env["R2_BUCKET_NAME"]

    ids = [r[0] for r in con.execute("""
        SELECT DISTINCT player_id FROM player_game_box WHERE played ORDER BY 1
    """).fetchall()]
    if limit:
        ids = ids[:limit]

    existing = set()
    if skip_existing:
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": "%s/players/" % PREFIX, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            for o in page.get("Contents", []):
                existing.add(o["Key"])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        print("  %d objects already in R2" % len(existing))

    print("  exporting %d players" % len(ids))
    logs = shots = skipped = 0
    bytes_ = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, pid in enumerate(ids, 1):
            kg = "%s/players/%s/gamelog.parquet" % (PREFIX, pid)
            ks = "%s/players/%s/shots.parquet" % (PREFIX, pid)

            if skip_existing and kg in existing:
                skipped += 1
            else:
                n = put_parquet(con, s3, bucket, GAMELOG_SQL % pid, kg, tmp)
                if n:
                    logs += 1
                    bytes_ += n

            if skip_existing and ks in existing:
                pass
            else:
                n = put_parquet(con, s3, bucket, SHOTS_SQL % pid, ks, tmp)
                if n:
                    shots += 1
                    bytes_ += n

            if i % 250 == 0:
                print("  %5d/%d  logs=%d shots=%d skipped=%d  %.1f MB"
                      % (i, len(ids), logs, shots, skipped, bytes_ / 1048576.0))

    print("\n  %d gamelogs, %d shot files, %.1f MB" % (logs, shots, bytes_ / 1048576.0))
    con.close()


def cmd_registry(env):
    con, s3 = db(), s3_client(env)
    with tempfile.TemporaryDirectory() as tmp:
        n = put_parquet(con, s3, env["R2_BUCKET_NAME"], REGISTRY_SQL,
                        "%s/registry/players.parquet" % PREFIX, tmp)
    rows = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    print("  registry: %d players, %.1f KB" % (rows, (n or 0) / 1024.0))
    con.close()


def cmd_manifest(env):
    con, s3 = db(), s3_client(env)
    bucket = env["R2_BUCKET_NAME"]
    seasons = con.execute("""
        SELECT season, COUNT(DISTINCT game_id) FROM games GROUP BY 1 ORDER BY 1 DESC
    """).fetchall()
    man = {
        "written_at": datetime.utcnow().isoformat() + "Z",
        "prefix": PREFIX,
        "seasons": [{"season": s, "games": g} for s, g in seasons],
        "players_with_games": con.execute(
            "SELECT COUNT(DISTINCT player_id) FROM player_game_box WHERE played"
        ).fetchone()[0],
        "box_rows": con.execute("SELECT COUNT(*) FROM player_game_box").fetchone()[0],
        "pbp_events": con.execute("SELECT COUNT(*) FROM play_by_play").fetchone()[0],
        "paths": {
            "registry": "%s/registry/players.parquet" % PREFIX,
            "player_season": "%s/seasons/{season}/player_season.parquet" % PREFIX,
            "team_season": "%s/seasons/{season}/team_season.parquet" % PREFIX,
            "gamelog": "%s/players/{player_id}/gamelog.parquet" % PREFIX,
            "shots": "%s/players/{player_id}/shots.parquet" % PREFIX,
        },
    }
    s3.put_object(Bucket=bucket, Key="%s/manifest.json" % PREFIX,
                  Body=json.dumps(man, indent=2).encode(),
                  ContentType="application/json")
    print("  manifest written: %d seasons, %d players, %d box rows, %d events"
          % (len(seasons), man["players_with_games"], man["box_rows"], man["pbp_events"]))
    con.close()


def cmd_status(env):
    con, s3 = db(), s3_client(env)
    bucket = env["R2_BUCKET_NAME"]
    counts, size = {}, {}
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            grp = parts[1] if len(parts) > 1 else "?"
            counts[grp] = counts.get(grp, 0) + 1
            size[grp] = size.get(grp, 0) + o["Size"]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    want_seasons = con.execute("SELECT COUNT(DISTINCT season) FROM games").fetchone()[0]
    want_players = con.execute(
        "SELECT COUNT(DISTINCT player_id) FROM player_game_box WHERE played").fetchone()[0]

    print("  in R2 under %s/:" % PREFIX)
    for k in sorted(counts):
        print("    %-12s %6d objects  %8.1f MB" % (k, counts[k], size[k] / 1048576.0))
    print("\n  expected: %d seasons x2 files = %d, %d players x2 = %d"
          % (want_seasons, want_seasons * 2, want_players, want_players * 2))
    con.close()


def cmd_verify(env, sample=20):
    import random
    con, s3 = db(), s3_client(env)
    bucket = env["R2_BUCKET_NAME"]
    ids = [r[0] for r in con.execute("""
        SELECT DISTINCT player_id FROM player_game_box WHERE played
    """).fetchall()]
    picks = random.sample(ids, min(sample, len(ids)))

    import duckdb
    tmpcon = duckdb.connect()
    ok = bad = miss = 0
    for pid in picks:
        key = "%s/players/%s/gamelog.parquet" % (PREFIX, pid)
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception:
            print("    MISSING %s" % key)
            miss += 1
            continue
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(body)
            path = f.name
        n_r2 = tmpcon.execute(
            "SELECT COUNT(*) FROM read_parquet('%s')" % path).fetchone()[0]
        os.unlink(path)
        n_db = con.execute(
            "SELECT COUNT(*) FROM player_game_box WHERE player_id=? AND played",
            [pid]).fetchone()[0]
        if n_r2 == n_db:
            ok += 1
        else:
            print("    MISMATCH %s: r2=%d db=%d" % (pid, n_r2, n_db))
            bad += 1
    print("  %d matched, %d mismatched, %d missing (of %d sampled)"
          % (ok, bad, miss, len(picks)))
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seasons", "players", "registry",
                                    "manifest", "all", "status", "verify"])
    ap.add_argument("--season")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--skip-existing", action="store_true")
    a = ap.parse_args()
    e = load_env()

    if a.cmd == "seasons":
        cmd_seasons(e, a.season)
    elif a.cmd == "players":
        cmd_players(e, a.limit, a.skip_existing)
    elif a.cmd == "registry":
        cmd_registry(e)
    elif a.cmd == "manifest":
        cmd_manifest(e)
    elif a.cmd == "status":
        cmd_status(e)
    elif a.cmd == "verify":
        cmd_verify(e, a.sample)
    else:
        cmd_registry(e)
        cmd_seasons(e)
        cmd_players(e, a.limit, a.skip_existing)
        cmd_manifest(e)
