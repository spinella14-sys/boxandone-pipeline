#!/usr/bin/env python3
"""
export_full.py — export to R2 from the FULL history, not just the hot database.

export_r2.py reads the local DuckDB. After archive_cold.py pruned 29 seasons,
that file holds only 2025-26, so running it rewrote every player's career file
with a single season. This reads the archived Parquet alongside the hot
database and unions them, which is what the nightly job will need too.

    python3 export_full.py sync        # pull archive parquet locally (one time)
    python3 export_full.py check       # confirm the union looks right
    python3 export_full.py seasons
    python3 export_full.py players
    python3 export_full.py registry
    python3 export_full.py all

The local archive cache lands in ~/boxandone/data/archive/ and is skipped on
later runs. It is a cache: delete it any time, it re-downloads.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"
ARCHIVE = "v2/archive"

# what the export actually needs out of the archive
NEEDED = ["games.parquet", "player_game_box.parquet", "play_by_play.parquet"]


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
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto",
                        config=Config(max_pool_connections=16,
                                      retries={"max_attempts": 3}))


def cmd_sync(env, force=False):
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    seasons, token = set(), None
    while True:
        kw = {"Bucket": bucket, "Prefix": ARCHIVE + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            p = o["Key"].split("/")
            if len(p) >= 3:
                seasons.add(p[2])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    total = skipped = 0
    for s in sorted(seasons):
        d = os.path.join(CACHE, s)
        os.makedirs(d, exist_ok=True)
        got = []
        for f in NEEDED:
            local = os.path.join(d, f)
            if os.path.exists(local) and not force:
                skipped += 1
                got.append("%s=cached" % f.split(".")[0])
                continue
            try:
                s3.download_file(bucket, "%s/%s/%s" % (ARCHIVE, s, f), local)
                total += os.path.getsize(local)
                got.append("%s=%.0fMB" % (f.split(".")[0],
                                          os.path.getsize(local) / 1048576.0))
            except Exception:
                got.append("%s=absent" % f.split(".")[0])
        print("  %-9s %s" % (s, "  ".join(got)))
    print("\n  downloaded %.0f MB, %d files already cached" % (total / 1048576.0, skipped))


def connect(env):
    """DuckDB with union views over archive + hot database."""
    import duckdb
    if not os.path.isdir(CACHE) or not os.listdir(CACHE):
        sys.exit("  archive cache empty — run: python3 export_full.py sync")

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)

    def glob(name):
        return os.path.join(CACHE, "*", name)

    # Archive and hot were written from the same tables, so column order matches.
    # BY NAME guards against a future column being added to one side only.
    con.execute("""
        CREATE VIEW all_games AS
        SELECT * FROM read_parquet('%s', union_by_name=true)
        UNION ALL BY NAME
        SELECT * FROM hot.games
    """ % glob("games.parquet"))

    con.execute("""
        CREATE VIEW all_box AS
        SELECT * FROM read_parquet('%s', union_by_name=true)
        UNION ALL BY NAME
        SELECT * FROM hot.player_game_box
    """ % glob("player_game_box.parquet"))

    con.execute("""
        CREATE VIEW all_pbp AS
        SELECT * FROM read_parquet('%s', union_by_name=true)
        UNION ALL BY NAME
        SELECT * FROM hot.play_by_play
    """ % glob("play_by_play.parquet"))

    return con


def cmd_check(env):
    con = connect(env)
    print("  games   %9d  across %d seasons"
          % (con.execute("SELECT COUNT(*) FROM all_games").fetchone()[0],
             con.execute("SELECT COUNT(DISTINCT season) FROM all_games").fetchone()[0]))
    print("  box     %9d  %d players"
          % (con.execute("SELECT COUNT(*) FROM all_box").fetchone()[0],
             con.execute("SELECT COUNT(DISTINCT player_id) FROM all_box WHERE played").fetchone()[0]))
    print("  pbp     %9d" % con.execute("SELECT COUNT(*) FROM all_pbp").fetchone()[0])
    print("\n  season types:")
    for r in con.execute("""SELECT season_type, COUNT(*) FROM all_games
                            GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        print("    %-12s %d" % r)
    print("\n  spot check — a full career:")
    for r in con.execute("""
        SELECT p.full_name, COUNT(DISTINCT g.season) AS seasons, COUNT(*) AS games
        FROM all_box b JOIN all_games g ON g.game_id=b.game_id
        JOIN hot.players p ON p.player_id=b.player_id
        WHERE b.played AND p.full_name IN ('Luka Dončić','LeBron James','Nikola Jokić')
        GROUP BY 1 ORDER BY 3 DESC""").fetchall():
        print("    %-22s %2d seasons, %4d games" % r)
    con.close()


# ---------------------------------------------------------------------------
# queries — identical shape to export_r2.py but over the union views
# ---------------------------------------------------------------------------

SEASON_SQL = """
WITH tov_types AS (
  SELECT e.player_id, g.season_type,
         CASE
             WHEN e.sub_type IN ('Bad Pass', 'Lost Ball') THEN 'live'
             WHEN e.sub_type IN ('Out of Bounds - Bad Pass Turnover',
                                 'Out of Bounds Lost Ball Turnover',
                                 'Step Out of Bounds Turnover') THEN 'oob'
             WHEN e.sub_type = 'Offensive Foul Turnover' THEN 'ofoul'
             WHEN e.sub_type IN ('Traveling', 'Double Dribble', 'Palming Turnover',
                                 'Shot Clock Turnover', 'Backcourt Turnover',
                                 '3 Second Violation', '5 Second Violation',
                                 '8 Second Violation', 'Offensive Goaltending')
               THEN 'viol'
             ELSE 'other'
           END AS bucket,
         e.sub_type IN ('Bad Pass', 'Out of Bounds - Bad Pass Turnover') AS is_pass,
         COUNT(*) AS n
  FROM all_pbp e
  JOIN all_games g ON g.game_id = e.game_id
  WHERE lower(e.action_type) LIKE '%%turnover%%'
    AND g.season = '%s' AND e.player_id IS NOT NULL
  GROUP BY 1,2,3,4
),
tov_wide AS (
  SELECT player_id, season_type,
         SUM(CASE WHEN bucket='live' THEN n ELSE 0 END) AS to_live,
         SUM(CASE WHEN bucket='oob' THEN n ELSE 0 END) AS to_oob,
         SUM(CASE WHEN bucket='ofoul' THEN n ELSE 0 END) AS to_ofoul,
         SUM(CASE WHEN bucket='viol' THEN n ELSE 0 END) AS to_viol,
         SUM(CASE WHEN bucket='other' THEN n ELSE 0 END) AS to_other,
         SUM(CASE WHEN is_pass THEN n ELSE 0 END) AS to_pass
  FROM tov_types GROUP BY 1,2
),
zones AS (
  SELECT e.player_id, g.season_type,
         CASE
             WHEN sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 <= 4
               THEN 'ra'
             WHEN e.shot_value = 2 AND abs(e.x_legacy) <= 80
                  AND e.y_legacy <= 142.5 THEN 'paint'
             WHEN e.shot_value = 2
                  AND sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 <= 14
               THEN 'short_mid'
             WHEN e.shot_value = 2 AND degrees(atan2(e.y_legacy, e.x_legacy)) < 60
               THEN 'long_mid_r'
             WHEN e.shot_value = 2 AND degrees(atan2(e.y_legacy, e.x_legacy)) > 120
               THEN 'long_mid_l'
             WHEN e.shot_value = 2 THEN 'long_mid_c'
             WHEN abs(e.x_legacy) >= 220 AND e.y_legacy <= 92.5
               THEN CASE WHEN e.x_legacy < 0 THEN 'c3_l' ELSE 'c3_r' END
             WHEN sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 >= 30
               THEN 'heave'
             WHEN degrees(atan2(e.y_legacy, e.x_legacy)) < 60  THEN 'atb_r'
             WHEN degrees(atan2(e.y_legacy, e.x_legacy)) > 120 THEN 'atb_l'
             ELSE 'atb_top'
           END AS zone,
         COUNT(*) AS fga,
         SUM(CASE WHEN e.shot_result = 'Made' THEN 1 ELSE 0 END) AS fgm
  FROM all_pbp e
  JOIN all_games g ON g.game_id = e.game_id
  WHERE e.is_field_goal AND g.season = '%s'
    AND e.player_id IS NOT NULL AND e.x_legacy IS NOT NULL
  GROUP BY 1,2,3
),
zone_wide AS (
  SELECT player_id, season_type,
         SUM(CASE WHEN zone='ra' THEN fga ELSE 0 END) AS ra_fga,
         SUM(CASE WHEN zone='ra' THEN fgm ELSE 0 END) AS ra_fgm,
         SUM(CASE WHEN zone='paint' THEN fga ELSE 0 END) AS paint_fga,
         SUM(CASE WHEN zone='paint' THEN fgm ELSE 0 END) AS paint_fgm,
         SUM(CASE WHEN zone='short_mid' THEN fga ELSE 0 END) AS short_mid_fga,
         SUM(CASE WHEN zone='short_mid' THEN fgm ELSE 0 END) AS short_mid_fgm,
         SUM(CASE WHEN zone='long_mid_l' THEN fga ELSE 0 END) AS long_mid_l_fga,
         SUM(CASE WHEN zone='long_mid_l' THEN fgm ELSE 0 END) AS long_mid_l_fgm,
         SUM(CASE WHEN zone='long_mid_c' THEN fga ELSE 0 END) AS long_mid_c_fga,
         SUM(CASE WHEN zone='long_mid_c' THEN fgm ELSE 0 END) AS long_mid_c_fgm,
         SUM(CASE WHEN zone='long_mid_r' THEN fga ELSE 0 END) AS long_mid_r_fga,
         SUM(CASE WHEN zone='long_mid_r' THEN fgm ELSE 0 END) AS long_mid_r_fgm,
         SUM(CASE WHEN zone='c3_l' THEN fga ELSE 0 END) AS c3_l_fga,
         SUM(CASE WHEN zone='c3_l' THEN fgm ELSE 0 END) AS c3_l_fgm,
         SUM(CASE WHEN zone='c3_r' THEN fga ELSE 0 END) AS c3_r_fga,
         SUM(CASE WHEN zone='c3_r' THEN fgm ELSE 0 END) AS c3_r_fgm,
         SUM(CASE WHEN zone='atb_l' THEN fga ELSE 0 END) AS atb_l_fga,
         SUM(CASE WHEN zone='atb_l' THEN fgm ELSE 0 END) AS atb_l_fgm,
         SUM(CASE WHEN zone='atb_top' THEN fga ELSE 0 END) AS atb_top_fga,
         SUM(CASE WHEN zone='atb_top' THEN fgm ELSE 0 END) AS atb_top_fgm,
         SUM(CASE WHEN zone='atb_r' THEN fga ELSE 0 END) AS atb_r_fga,
         SUM(CASE WHEN zone='atb_r' THEN fgm ELSE 0 END) AS atb_r_fgm,
         SUM(CASE WHEN zone='heave' THEN fga ELSE 0 END) AS heave_fga,
         SUM(CASE WHEN zone='heave' THEN fgm ELSE 0 END) AS heave_fgm
  FROM zones GROUP BY 1,2
)
SELECT b.player_id, p.full_name, g.season, g.league, g.season_type,
       COUNT(DISTINCT b.team_abbr) AS teams, MAX(b.team_abbr) AS last_team,
       COUNT(*) AS gp, SUM(CASE WHEN b.started THEN 1 ELSE 0 END) AS gs,
       SUM(b.seconds_played)/60.0 AS mp,
       SUM(b.fgm) fgm, SUM(b.fga) fga, SUM(b.fg3m) fg3m, SUM(b.fg3a) fg3a,
       SUM(b.ftm) ftm, SUM(b.fta) fta, SUM(b.orb) orb, SUM(b.drb) drb,
       SUM(b.trb) trb, SUM(b.ast) ast, SUM(b.stl) stl, SUM(b.blk) blk,
       SUM(b.tov) tov, SUM(b.pf) pf, SUM(b.pts) pts, SUM(b.plus_minus) plus_minus,
       AVG(date_diff('day', p.birthdate, g.game_date)/365.25) AS age,
       MIN(g.game_date) first_game, MAX(g.game_date) last_game,
       z.ra_fga, z.ra_fgm,
       z.paint_fga, z.paint_fgm,
       z.short_mid_fga, z.short_mid_fgm,
       z.long_mid_l_fga, z.long_mid_l_fgm,
       z.long_mid_c_fga, z.long_mid_c_fgm,
       z.long_mid_r_fga, z.long_mid_r_fgm,
       z.c3_l_fga, z.c3_l_fgm,
       z.c3_r_fga, z.c3_r_fgm,
       z.atb_l_fga, z.atb_l_fgm,
       z.atb_top_fga, z.atb_top_fgm,
       z.atb_r_fga, z.atb_r_fgm,
       z.heave_fga, z.heave_fgm,
       v.to_live,
       v.to_oob,
       v.to_ofoul,
       v.to_viol,
       v.to_other,
       v.to_pass,
FROM all_box b
JOIN all_games g ON g.game_id = b.game_id
JOIN hot.players p ON p.player_id = b.player_id
LEFT JOIN zone_wide z
  ON z.player_id = b.player_id AND z.season_type = g.season_type
LEFT JOIN tov_wide v
  ON v.player_id = b.player_id AND v.season_type = g.season_type
WHERE b.played AND g.season = '%s'
GROUP BY 1,2,3,4,5,
         z.ra_fga, z.ra_fgm,
         z.paint_fga, z.paint_fgm,
         z.short_mid_fga, z.short_mid_fgm,
         z.long_mid_l_fga, z.long_mid_l_fgm,
         z.long_mid_c_fga, z.long_mid_c_fgm,
         z.long_mid_r_fga, z.long_mid_r_fgm,
         z.c3_l_fga, z.c3_l_fgm,
         z.c3_r_fga, z.c3_r_fgm,
         z.atb_l_fga, z.atb_l_fgm,
         z.atb_top_fga, z.atb_top_fgm,
         z.atb_r_fga, z.atb_r_fgm,
         z.heave_fga, z.heave_fgm,
         v.to_live,
         v.to_oob,
         v.to_ofoul,
         v.to_viol,
         v.to_other,
         v.to_pass
"""

TEAM_SQL = """
WITH own AS (
  SELECT g.season, g.league, g.season_type, b.team_abbr AS team_abbr,
         COUNT(DISTINCT g.game_id) AS gp,
         SUM(b.seconds_played)/60.0 AS team_mp,
         SUM(b.fgm) fgm, SUM(b.fga) fga, SUM(b.fg3m) fg3m, SUM(b.fg3a) fg3a,
         SUM(b.ftm) ftm, SUM(b.fta) fta,
         SUM(b.orb) orb, SUM(b.drb) drb, SUM(b.trb) trb,
         SUM(b.ast) ast, SUM(b.stl) stl, SUM(b.blk) blk,
         SUM(b.tov) tov, SUM(b.pf) pf, SUM(b.pts) pts
  FROM all_box b JOIN all_games g ON g.game_id = b.game_id
  WHERE b.played AND g.season = '%s'
  GROUP BY 1,2,3,4
),
opp AS (
  -- same rows, grouped by who the line was scored AGAINST
  SELECT g.season, g.league, g.season_type, b.opp_abbr AS team_abbr,
         SUM(b.fgm) opp_fgm, SUM(b.fga) opp_fga,
         SUM(b.fg3m) opp_fg3m, SUM(b.fg3a) opp_fg3a,
         SUM(b.ftm) opp_ftm, SUM(b.fta) opp_fta,
         SUM(b.orb) opp_orb, SUM(b.drb) opp_drb, SUM(b.trb) opp_trb,
         SUM(b.ast) opp_ast, SUM(b.stl) opp_stl, SUM(b.blk) opp_blk,
         SUM(b.tov) opp_tov, SUM(b.pf) opp_pf, SUM(b.pts) opp_pts
  FROM all_box b JOIN all_games g ON g.game_id = b.game_id
  WHERE b.played AND g.season = '%s' AND b.opp_abbr IS NOT NULL
  GROUP BY 1,2,3,4
),
wl AS (
  -- one row per team per game, so a win is just a score comparison
  SELECT season, league, season_type, team_abbr,
         SUM(CASE WHEN won THEN 1 ELSE 0 END) AS w,
         SUM(CASE WHEN won THEN 0 ELSE 1 END) AS l
  FROM (
    SELECT season, league, season_type, home_abbr AS team_abbr,
           home_score > away_score AS won
    FROM all_games
    WHERE season = '%s' AND home_score IS NOT NULL AND home_abbr IS NOT NULL
    UNION ALL
    SELECT season, league, season_type, away_abbr AS team_abbr,
           away_score > home_score AS won
    FROM all_games
    WHERE season = '%s' AND away_score IS NOT NULL AND away_abbr IS NOT NULL
  )
  GROUP BY 1,2,3,4
)
SELECT o.*,
       p.opp_fgm, p.opp_fga, p.opp_fg3m, p.opp_fg3a, p.opp_ftm, p.opp_fta,
       p.opp_orb, p.opp_drb, p.opp_trb, p.opp_ast, p.opp_stl, p.opp_blk,
       p.opp_tov, p.opp_pf, p.opp_pts,
       -- Oliver possession estimate, both ends. Averaging the two sides is the
       -- convention: each team's own estimate carries its own rebounding noise.
       o.fga + 0.44*o.fta - o.orb + o.tov                         AS poss_raw,
       p.opp_fga + 0.44*p.opp_fta - p.opp_orb + p.opp_tov         AS opp_poss_raw,
       ((o.fga + 0.44*o.fta - o.orb + o.tov)
        + (p.opp_fga + 0.44*p.opp_fta - p.opp_orb + p.opp_tov)) / 2.0 AS poss_est,
       w.w AS wins, w.l AS losses,
       CAST(w.w AS DOUBLE) / NULLIF(w.w + w.l, 0) AS win_pct,
       o.pts - p.opp_pts AS point_diff,
       (o.pts - p.opp_pts) / NULLIF(CAST(o.gp AS DOUBLE), 0) AS mov,
       CASE
           WHEN o.season < '2004-05' THEN CASE o.team_abbr
             WHEN 'BOS' THEN 'East'
             WHEN 'MIA' THEN 'East'
             WHEN 'NJN' THEN 'East'
             WHEN 'BRK' THEN 'East'
             WHEN 'NYK' THEN 'East'
             WHEN 'ORL' THEN 'East'
             WHEN 'PHI' THEN 'East'
             WHEN 'WAS' THEN 'East'
             WHEN 'WSB' THEN 'East'
             WHEN 'ATL' THEN 'East'
             WHEN 'CHH' THEN 'East'
             WHEN 'NOH' THEN 'East'
             WHEN 'CHI' THEN 'East'
             WHEN 'CLE' THEN 'East'
             WHEN 'DET' THEN 'East'
             WHEN 'IND' THEN 'East'
             WHEN 'MIL' THEN 'East'
             WHEN 'TOR' THEN 'East'
             WHEN 'DAL' THEN 'West'
             WHEN 'DEN' THEN 'West'
             WHEN 'HOU' THEN 'West'
             WHEN 'MIN' THEN 'West'
             WHEN 'SAS' THEN 'West'
             WHEN 'UTA' THEN 'West'
             WHEN 'VAN' THEN 'West'
             WHEN 'MEM' THEN 'West'
             WHEN 'GSW' THEN 'West'
             WHEN 'LAC' THEN 'West'
             WHEN 'LAL' THEN 'West'
             WHEN 'PHX' THEN 'West'
             WHEN 'PHO' THEN 'West'
             WHEN 'POR' THEN 'West'
             WHEN 'SAC' THEN 'West'
             WHEN 'SEA' THEN 'West'
             ELSE NULL END
           ELSE CASE o.team_abbr
             WHEN 'BOS' THEN 'East'
             WHEN 'NJN' THEN 'East'
             WHEN 'BRK' THEN 'East'
             WHEN 'BKN' THEN 'East'
             WHEN 'NYK' THEN 'East'
             WHEN 'PHI' THEN 'East'
             WHEN 'TOR' THEN 'East'
             WHEN 'CHI' THEN 'East'
             WHEN 'CLE' THEN 'East'
             WHEN 'DET' THEN 'East'
             WHEN 'IND' THEN 'East'
             WHEN 'MIL' THEN 'East'
             WHEN 'ATL' THEN 'East'
             WHEN 'CHA' THEN 'East'
             WHEN 'CHO' THEN 'East'
             WHEN 'MIA' THEN 'East'
             WHEN 'ORL' THEN 'East'
             WHEN 'WAS' THEN 'East'
             WHEN 'DEN' THEN 'West'
             WHEN 'MIN' THEN 'West'
             WHEN 'SEA' THEN 'West'
             WHEN 'OKC' THEN 'West'
             WHEN 'POR' THEN 'West'
             WHEN 'UTA' THEN 'West'
             WHEN 'GSW' THEN 'West'
             WHEN 'LAC' THEN 'West'
             WHEN 'LAL' THEN 'West'
             WHEN 'PHX' THEN 'West'
             WHEN 'PHO' THEN 'West'
             WHEN 'SAC' THEN 'West'
             WHEN 'DAL' THEN 'West'
             WHEN 'HOU' THEN 'West'
             WHEN 'MEM' THEN 'West'
             WHEN 'NOH' THEN 'West'
             WHEN 'NOK' THEN 'West'
             WHEN 'NOP' THEN 'West'
             WHEN 'SAS' THEN 'West'
             ELSE NULL END
           END AS conference,
       CASE
           WHEN o.season < '2004-05' THEN CASE o.team_abbr
             WHEN 'BOS' THEN 'Atlantic'
             WHEN 'MIA' THEN 'Atlantic'
             WHEN 'NJN' THEN 'Atlantic'
             WHEN 'BRK' THEN 'Atlantic'
             WHEN 'NYK' THEN 'Atlantic'
             WHEN 'ORL' THEN 'Atlantic'
             WHEN 'PHI' THEN 'Atlantic'
             WHEN 'WAS' THEN 'Atlantic'
             WHEN 'WSB' THEN 'Atlantic'
             WHEN 'ATL' THEN 'Central'
             WHEN 'CHH' THEN 'Central'
             WHEN 'NOH' THEN 'Central'
             WHEN 'CHI' THEN 'Central'
             WHEN 'CLE' THEN 'Central'
             WHEN 'DET' THEN 'Central'
             WHEN 'IND' THEN 'Central'
             WHEN 'MIL' THEN 'Central'
             WHEN 'TOR' THEN 'Central'
             WHEN 'DAL' THEN 'Midwest'
             WHEN 'DEN' THEN 'Midwest'
             WHEN 'HOU' THEN 'Midwest'
             WHEN 'MIN' THEN 'Midwest'
             WHEN 'SAS' THEN 'Midwest'
             WHEN 'UTA' THEN 'Midwest'
             WHEN 'VAN' THEN 'Midwest'
             WHEN 'MEM' THEN 'Midwest'
             WHEN 'GSW' THEN 'Pacific'
             WHEN 'LAC' THEN 'Pacific'
             WHEN 'LAL' THEN 'Pacific'
             WHEN 'PHX' THEN 'Pacific'
             WHEN 'PHO' THEN 'Pacific'
             WHEN 'POR' THEN 'Pacific'
             WHEN 'SAC' THEN 'Pacific'
             WHEN 'SEA' THEN 'Pacific'
             ELSE NULL END
           ELSE CASE o.team_abbr
             WHEN 'BOS' THEN 'Atlantic'
             WHEN 'NJN' THEN 'Atlantic'
             WHEN 'BRK' THEN 'Atlantic'
             WHEN 'BKN' THEN 'Atlantic'
             WHEN 'NYK' THEN 'Atlantic'
             WHEN 'PHI' THEN 'Atlantic'
             WHEN 'TOR' THEN 'Atlantic'
             WHEN 'CHI' THEN 'Central'
             WHEN 'CLE' THEN 'Central'
             WHEN 'DET' THEN 'Central'
             WHEN 'IND' THEN 'Central'
             WHEN 'MIL' THEN 'Central'
             WHEN 'ATL' THEN 'Southeast'
             WHEN 'CHA' THEN 'Southeast'
             WHEN 'CHO' THEN 'Southeast'
             WHEN 'MIA' THEN 'Southeast'
             WHEN 'ORL' THEN 'Southeast'
             WHEN 'WAS' THEN 'Southeast'
             WHEN 'DEN' THEN 'Northwest'
             WHEN 'MIN' THEN 'Northwest'
             WHEN 'SEA' THEN 'Northwest'
             WHEN 'OKC' THEN 'Northwest'
             WHEN 'POR' THEN 'Northwest'
             WHEN 'UTA' THEN 'Northwest'
             WHEN 'GSW' THEN 'Pacific'
             WHEN 'LAC' THEN 'Pacific'
             WHEN 'LAL' THEN 'Pacific'
             WHEN 'PHX' THEN 'Pacific'
             WHEN 'PHO' THEN 'Pacific'
             WHEN 'SAC' THEN 'Pacific'
             WHEN 'DAL' THEN 'Southwest'
             WHEN 'HOU' THEN 'Southwest'
             WHEN 'MEM' THEN 'Southwest'
             WHEN 'NOH' THEN 'Southwest'
             WHEN 'NOK' THEN 'Southwest'
             WHEN 'NOP' THEN 'Southwest'
             WHEN 'SAS' THEN 'Southwest'
             ELSE NULL END
           END AS division
FROM own o
JOIN opp p
  ON p.season = o.season AND p.league = o.league
 AND p.season_type = o.season_type AND p.team_abbr = o.team_abbr
LEFT JOIN wl w
  ON w.season = o.season AND w.league = o.league
 AND w.season_type = o.season_type AND w.team_abbr = o.team_abbr
"""

PLAYER_GAME_SQL = """
SELECT b.player_id, p.full_name,
       b.game_id, g.game_date, g.season, g.league, g.season_type,
       b.team_abbr, b.opp_abbr, b.is_home, b.started,
       b.seconds_played/60.0 AS mp,
       b.fgm, b.fga, b.fg3m, b.fg3a, b.ftm, b.fta,
       b.orb, b.drb, b.trb, b.ast, b.stl, b.blk, b.tov, b.pf, b.pts,
       b.plus_minus,
       date_diff('day', p.birthdate, g.game_date)/365.25 AS age
FROM all_box b
JOIN all_games g ON g.game_id = b.game_id
JOIN hot.players p ON p.player_id = b.player_id
WHERE b.played AND g.season = '%s'
ORDER BY g.game_date, b.team_abbr, b.player_id
"""

GAMELOG_SQL = """
SELECT b.game_id, g.game_date, g.season, g.league, g.season_type,
       b.team_abbr, b.opp_abbr, b.is_home, b.started,
       b.seconds_played/60.0 AS mp,
       b.fgm, b.fga, b.fg3m, b.fg3a, b.ftm, b.fta,
       b.orb, b.drb, b.trb, b.ast, b.stl, b.blk, b.tov, b.pf, b.pts,
       b.plus_minus, g.home_score, g.away_score,
       date_diff('day', p.birthdate, g.game_date)/365.25 AS age
FROM all_box b
JOIN all_games g ON g.game_id = b.game_id
JOIN hot.players p ON p.player_id = b.player_id
WHERE b.player_id = '%s' AND b.played
ORDER BY g.game_date
"""

SHOTS_SQL = """
SELECT e.game_id, g.game_date, g.season, g.season_type,
       e.period, e.seconds_left, e.elapsed_seconds, e.team_abbr,
       e.shot_result, e.shot_value, e.shot_distance,
       e.x_legacy, e.y_legacy, e.sub_type AS shot_type, e.description
FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
WHERE e.player_id = '%s' AND e.is_field_goal
ORDER BY g.game_date, e.action_id
"""

REGISTRY_SQL = """
SELECT p.player_id, p.full_name, p.display_name, p.name_normalized,
       -- the registry first, the scrape second: a value already here was put
       -- there by a better-trusted source, so enrichment fills blanks only
       COALESCE(p.birthdate, bio.birthdate) AS birthdate,
       CASE WHEN p.birthdate IS NULL AND bio.birthdate IS NOT NULL
            THEN 'confirmed' ELSE p.birthdate_status END AS birthdate_status,
       p.position,
       COALESCE(p.height_in, bio.height_in) AS height_in,
       COALESCE(p.weight_lb, bio.weight_lb) AS weight_lb,
       COALESCE(p.college, bio.college) AS college,
       COALESCE(p.draft_year, bio.draft_year) AS draft_year,
       COALESCE(p.draft_round, bio.draft_round) AS draft_round,
       COALESCE(p.draft_pick, bio.draft_pick) AS draft_pick,
       p.status,
       COALESCE(p.nationality, bio.country) AS nationality,
       i.source_id AS bbref_id, n.source_id AS nba_id,
       s.seasons, s.first_season, s.last_season, s.career_gp, s.career_pts
FROM hot.players p
LEFT JOIN hot.player_identifiers i ON i.player_id=p.player_id AND i.source='bbref'
LEFT JOIN hot.player_identifiers n ON n.player_id=p.player_id AND n.source='nba'
LEFT JOIN hot.player_bio bio ON bio.player_id=p.player_id AND bio.source='nba'
LEFT JOIN (
    SELECT b.player_id, COUNT(DISTINCT g.season) seasons,
           MIN(g.season) first_season, MAX(g.season) last_season,
           COUNT(*) career_gp, SUM(b.pts) career_pts
    FROM all_box b JOIN all_games g ON g.game_id=b.game_id
    WHERE b.played GROUP BY 1
) s ON s.player_id = p.player_id
"""


def put(con, s3, bucket, sql, key, tmp):
    path = os.path.join(tmp, "o.parquet")
    if os.path.exists(path):
        os.remove(path)
    con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (sql, path))
    body = open(path, "rb").read()
    if len(body) < 100:
        return None
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    return len(body)


def cmd_seasons(env, only=None):
    con, s3 = connect(env), s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    seasons = [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM all_games ORDER BY season DESC").fetchall()]
    if only:
        missing = [s for s in only if s not in seasons]
        if missing:
            sys.exit("  no games for season(s): %s" % ", ".join(missing))
        seasons = [s for s in seasons if s in only]
    tot = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            a = put(con, s3, bucket, SEASON_SQL % (s, s, s),
                    "%s/seasons/%s/player_season.parquet" % (PREFIX, s), tmp)
            b = put(con, s3, bucket, TEAM_SQL % (s, s, s, s),
                    "%s/seasons/%s/team_season.parquet" % (PREFIX, s), tmp)
            c = put(con, s3, bucket, PLAYER_GAME_SQL % s,
                    "%s/seasons/%s/player_game.parquet" % (PREFIX, s), tmp)
            tot += (a or 0) + (b or 0) + (c or 0)
            print("  %-9s agg %5.0f KB   team %4.0f KB   games %6.0f KB"
                  % (s, (a or 0) / 1024.0, (b or 0) / 1024.0, (c or 0) / 1024.0))
    print("\n  %d seasons, %.1f MB" % (len(seasons), tot / 1048576.0))
    con.close()


def cmd_players(env, limit=None):
    con, s3 = connect(env), s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT player_id FROM all_box WHERE played ORDER BY 1").fetchall()]
    if limit:
        ids = ids[:limit]
    print("  exporting %d players" % len(ids))
    logs = shots = 0
    size = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, pid in enumerate(ids, 1):
            n = put(con, s3, bucket, GAMELOG_SQL % pid,
                    "%s/players/%s/gamelog.parquet" % (PREFIX, pid), tmp)
            if n:
                logs += 1
                size += n
            n = put(con, s3, bucket, SHOTS_SQL % pid,
                    "%s/players/%s/shots.parquet" % (PREFIX, pid), tmp)
            if n:
                shots += 1
                size += n
            if i % 200 == 0:
                print("  %5d/%d  logs=%d shots=%d  %.0f MB"
                      % (i, len(ids), logs, shots, size / 1048576.0))
    print("\n  %d gamelogs, %d shot files, %.1f MB" % (logs, shots, size / 1048576.0))
    con.close()


def cmd_registry(env):
    con, s3 = connect(env), s3c(env)
    with tempfile.TemporaryDirectory() as tmp:
        n = put(con, s3, env["R2_BUCKET_NAME"], REGISTRY_SQL,
                "%s/registry/players.parquet" % PREFIX, tmp)
    print("  registry %.1f KB" % ((n or 0) / 1024.0))
    con.close()


def cmd_manifest(env):
    import json
    from datetime import datetime
    con, s3 = connect(env), s3c(env)
    seasons = con.execute("""SELECT season, COUNT(DISTINCT game_id)
                             FROM all_games GROUP BY 1 ORDER BY 1 DESC""").fetchall()
    man = {
        "written_at": datetime.utcnow().isoformat() + "Z",
        "prefix": PREFIX,
        "seasons": [{"season": s, "games": g} for s, g in seasons],
        "players_with_games": con.execute(
            "SELECT COUNT(DISTINCT player_id) FROM all_box WHERE played").fetchone()[0],
        "box_rows": con.execute("SELECT COUNT(*) FROM all_box").fetchone()[0],
        "pbp_events": con.execute("SELECT COUNT(*) FROM all_pbp").fetchone()[0],
        "paths": {
            "registry": "%s/registry/players.parquet" % PREFIX,
            "player_season": "%s/seasons/{season}/player_season.parquet" % PREFIX,
            "team_season": "%s/seasons/{season}/team_season.parquet" % PREFIX,
            "gamelog": "%s/players/{player_id}/gamelog.parquet" % PREFIX,
            "shots": "%s/players/{player_id}/shots.parquet" % PREFIX,
        },
    }
    s3.put_object(Bucket=env["R2_BUCKET_NAME"], Key="%s/manifest.json" % PREFIX,
                  Body=json.dumps(man, indent=2).encode(),
                  ContentType="application/json")
    print("  manifest: %d seasons, %d players, %d box rows, %d events"
          % (len(seasons), man["players_with_games"], man["box_rows"], man["pbp_events"]))
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "check", "seasons", "players",
                                    "registry", "manifest", "all"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--season", action="append",
                    help="limit `seasons` to this season; repeatable")
    a = ap.parse_args()
    e = load_env()

    if a.cmd == "sync":
        cmd_sync(e, a.force)
    elif a.cmd == "check":
        cmd_check(e)
    elif a.cmd == "seasons":
        cmd_seasons(e, a.season)
    elif a.cmd == "players":
        cmd_players(e, a.limit)
    elif a.cmd == "registry":
        cmd_registry(e)
    elif a.cmd == "manifest":
        cmd_manifest(e)
    else:
        cmd_registry(e)
        cmd_seasons(e, a.season)
        cmd_players(e, a.limit)
        cmd_manifest(e)
