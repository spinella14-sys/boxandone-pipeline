#!/usr/bin/env python3
"""
parse_assists.py — who assisted every made field goal, back to 1996-97.

NBA play-by-play writes the assist into the made shot's description, as the
passer's surname and his running total: "Curry 26' 3PT Pull-Up (3 PTS)
(Green 4 AST)". There is no assist id on the row, so the surname is matched
against the shooter's teammates in that same game — nine people at most,
usually one name — and resolved to our player id.

Matching: the play-by-play surname ("Green") or first initial + surname
("D. Green"), with accents and punctuation folded, against the shooter's
teammates in that game — both those named in play-by-play and everyone in
that side's box score, which catches players whose NBA id is not bridged.
Two teammates who share a surname and an initial cannot be told apart from
the text; those are kept as assisted with no passer ("ambiguous") rather
than guessed. Misses never carry a passer in this data, so only makes are
classified.

Results:
  seasons held in the hot database   ->  table shot_assists
  archived seasons                   ->  data/archive/{season}/shot_assists.parquet,
                                         also uploaded to v2/archive/{season}/

and, per season, v2/seasons/{season}/assisted.parquet — per player and
season type, by the same eleven zones as the Stat Database:
  {zone}_fgm_ast   his made shots that were assisted
  {zone}_ast       assists he threw into that zone

    python3 parse_assists.py check --season 2024-25     # match rates, writes nothing
    python3 parse_assists.py check --all
    python3 parse_assists.py build --season 2024-25
    python3 parse_assists.py build --all
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"

ZONES = ["ra", "paint", "short_mid", "long_mid_l", "long_mid_c", "long_mid_r",
         "c3_l", "c3_r", "atb_l", "atb_top", "atb_r", "heave"]

# identical to the zone CASE in export_full.SEASON_SQL, so the eleven zones
# here are the eleven zones in the Stat Database
ZONE_CASE = """CASE
    WHEN sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 <= 4 THEN 'ra'
    WHEN e.shot_value = 2 AND abs(e.x_legacy) <= 80 AND e.y_legacy <= 142.5 THEN 'paint'
    WHEN e.shot_value = 2
         AND sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 <= 14 THEN 'short_mid'
    WHEN e.shot_value = 2 AND degrees(atan2(e.y_legacy, e.x_legacy)) < 60 THEN 'long_mid_r'
    WHEN e.shot_value = 2 AND degrees(atan2(e.y_legacy, e.x_legacy)) > 120 THEN 'long_mid_l'
    WHEN e.shot_value = 2 THEN 'long_mid_c'
    WHEN abs(e.x_legacy) >= 220 AND e.y_legacy <= 92.5
      THEN CASE WHEN e.x_legacy < 0 THEN 'c3_l' ELSE 'c3_r' END
    WHEN sqrt(e.x_legacy*e.x_legacy + e.y_legacy*e.y_legacy)/10.0 >= 30 THEN 'heave'
    WHEN degrees(atan2(e.y_legacy, e.x_legacy)) < 60 THEN 'atb_r'
    WHEN degrees(atan2(e.y_legacy, e.x_legacy)) > 120 THEN 'atb_l'
    ELSE 'atb_top'
  END"""

RESOLVE_SQL = """
WITH made AS (
  SELECT e.game_id, e.action_id, e.team_abbr, e.player_id AS shooter,
         e.player_name AS shooter_name, e.period, e.shot_value, e.shot_distance,
         e.x_legacy, e.y_legacy, e.sub_type, e.description, e.location,
         regexp_extract(e.description, '\\(([^()]*?)\\s+\\d+\\s+AST\\)', 1) AS ast_raw
  FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
  WHERE g.season = '{season}' AND e.is_field_goal AND e.shot_result = 'Made'
),
pool AS (
  SELECT DISTINCT e.game_id, e.team_abbr, e.player_id, e.player_name, p.full_name
  FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
  LEFT JOIN hot.players p ON p.player_id = e.player_id
  WHERE g.season = '{season}' AND e.player_id IS NOT NULL AND e.player_name IS NOT NULL
),
keys AS (
  SELECT game_id, team_abbr, player_id, bo_norm(player_name) AS k FROM pool
  UNION
  SELECT game_id, team_abbr, player_id, bo_norm(substr(full_name, 1, 1) || player_name)
  FROM pool WHERE full_name IS NOT NULL
),
-- The box score lists everyone who played, including players whose NBA id is
-- not bridged and so carry no player_id in play-by-play. Matched by side of
-- the floor (home/away) rather than team code, which differs between sources.
boxkeys AS (
  SELECT b.game_id, b.is_home, b.player_id,
         bo_norm(regexp_replace(p.full_name, '^\\S+\\s+', '')) AS k
  FROM all_box b JOIN all_games g ON g.game_id = b.game_id
  JOIN hot.players p ON p.player_id = b.player_id
  WHERE g.season = '{season}' AND b.played
  UNION
  SELECT b.game_id, b.is_home, b.player_id,
         bo_norm(substr(p.full_name, 1, 1) || regexp_replace(p.full_name, '^\\S+\\s+', ''))
  FROM all_box b JOIN all_games g ON g.game_id = b.game_id
  JOIN hot.players p ON p.player_id = b.player_id
  WHERE g.season = '{season}' AND b.played
),
hits AS (
  SELECT m.game_id, m.action_id, k.player_id
  FROM made m
  JOIN keys k ON k.game_id = m.game_id AND k.team_abbr = m.team_abbr
             AND k.k = bo_norm(m.ast_raw) AND k.player_id <> coalesce(m.shooter, '')
  WHERE m.ast_raw <> ''
  UNION
  SELECT m.game_id, m.action_id, k.player_id
  FROM made m
  JOIN boxkeys k ON k.game_id = m.game_id AND k.is_home = (m.location = 'h')
             AND k.k = bo_norm(m.ast_raw) AND k.player_id <> coalesce(m.shooter, '')
  WHERE m.ast_raw <> '' AND m.location IN ('h', 'v')
),
cand AS (
  SELECT game_id, action_id, COUNT(DISTINCT player_id) AS n, MIN(player_id) AS pid
  FROM hits GROUP BY 1, 2
)
SELECT m.game_id, m.action_id,
       (m.ast_raw <> '') AS assisted,
       CASE WHEN c.n = 1 THEN c.pid END AS assist_player_id,
       NULLIF(m.ast_raw, '') AS assist_name,
       CASE WHEN m.ast_raw = '' THEN 'none'
            WHEN c.n = 1 THEN 'resolved'
            WHEN c.n > 1 THEN 'ambiguous'
            ELSE 'unmatched' END AS match,
       -- the shot itself rides along, so a passer's assist chart is read
       -- from this table alone rather than joined back to play-by-play
       m.team_abbr, m.shooter AS shooter_id, m.shooter_name, m.period,
       m.shot_value, m.shot_distance, m.x_legacy, m.y_legacy,
       m.sub_type AS shot_type, m.description
FROM made m LEFT JOIN cand c ON c.game_id = m.game_id AND c.action_id = m.action_id
"""


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
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def connect():
    """Read-only views over the archive cache and the hot database."""
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    con.execute("""CREATE MACRO bo_norm(x) AS
                   regexp_replace(lower(strip_accents(coalesce(x, ''))), '[^a-z]', '', 'g')""")
    have_cache = os.path.isdir(CACHE) and os.listdir(CACHE)
    g = os.path.join(CACHE, "*", "%s")
    if have_cache:
        con.execute("""CREATE VIEW all_games AS
            SELECT * FROM read_parquet('%s', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.games""" % (g % "games.parquet"))
        con.execute("""CREATE VIEW all_pbp AS
            SELECT * FROM read_parquet('%s', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.play_by_play""" % (g % "play_by_play.parquet"))
    else:
        print("  no archive cache — hot database only")
        con.execute("CREATE VIEW all_games AS SELECT * FROM hot.games")
        con.execute("CREATE VIEW all_pbp AS SELECT * FROM hot.play_by_play")
    if have_cache:
        con.execute("""CREATE VIEW all_box AS
            SELECT * FROM read_parquet('%s', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.player_game_box""" % (g % "player_game_box.parquet"))
    else:
        con.execute("CREATE VIEW all_box AS SELECT * FROM hot.player_game_box")
    add_ast_view(con)
    return con


AST_COLS = ("game_id VARCHAR, action_id BIGINT, assisted BOOLEAN, "
            "assist_player_id VARCHAR, assist_name VARCHAR, match VARCHAR, "
            "team_abbr VARCHAR, shooter_id VARCHAR, shooter_name VARCHAR, "
            "period SMALLINT, shot_value SMALLINT, shot_distance DOUBLE, "
            "x_legacy INTEGER, y_legacy INTEGER, shot_type VARCHAR, description VARCHAR")


def add_ast_view(con, cache=CACHE):
    """all_ast: archive parquet + hot.shot_assists, whichever exist. Shared
    with export_full.py, export_game_files.py and nightly2.py."""
    parts = []
    if os.path.isdir(cache) and any(
            os.path.exists(os.path.join(cache, d, "shot_assists.parquet"))
            for d in os.listdir(cache)):
        parts.append("SELECT * FROM read_parquet('%s', union_by_name=true)"
                     % os.path.join(cache, "*", "shot_assists.parquet"))
    have_hot = con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name='hot' AND table_name='shot_assists'").fetchone()[0]
    if have_hot:
        parts.append("SELECT * FROM hot.shot_assists")
    if not parts:
        cols = ", ".join("NULL::%s AS %s" % (c.split()[1], c.split()[0])
                         for c in AST_COLS.split(", "))
        parts.append("SELECT %s WHERE false" % cols)
    con.execute("CREATE OR REPLACE VIEW all_ast AS %s" % " UNION ALL BY NAME ".join(parts))


def seasons(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM all_games ORDER BY season DESC").fetchall()]


def hot_seasons(con):
    return {r[0] for r in con.execute("SELECT DISTINCT season FROM hot.games").fetchall()}


def cmd_check(con, targets):
    print("  %-9s %8s %8s %8s %8s %8s %7s" % ("season", "made", "ast", "resolved",
                                              "ambig", "unmatch", "ast%"))
    for s in targets:
        rows = dict(con.execute("SELECT match, COUNT(*) FROM (%s) GROUP BY 1"
                                % RESOLVE_SQL.format(season=s)).fetchall())
        made = sum(rows.values())
        if not made:
            print("  %-9s   no made field goals in play-by-play" % s)
            continue
        ast = made - rows.get("none", 0)
        print("  %-9s %8d %8d %8d %8d %8d %6.1f%%"
              % (s, made, ast, rows.get("resolved", 0), rows.get("ambiguous", 0),
                 rows.get("unmatched", 0), 100.0 * ast / made))
    # a few that did not resolve, so the pattern can be checked by eye
    if len(targets) == 1:
        bad = con.execute("""SELECT r.assist_name, e.team_abbr, e.description
            FROM (%s) r JOIN all_pbp e ON e.game_id = r.game_id AND e.action_id = r.action_id
            WHERE r.match IN ('unmatched', 'ambiguous') LIMIT 8"""
                          % RESOLVE_SQL.format(season=targets[0])).fetchall()
        if bad:
            print("\n  unresolved examples:")
            for name, team, desc in bad:
                print("    %-16s %-4s %s" % (name, team, (desc or "")[:90]))


ASSISTED_SQL = """
WITH s AS (
  SELECT e.player_id AS shooter, a.assist_player_id AS passer, g.season_type,
         a.assisted, {zone} AS zone
  FROM all_pbp e
  JOIN all_games g ON g.game_id = e.game_id
  JOIN all_ast a ON a.game_id = e.game_id AND a.action_id = e.action_id
  WHERE g.season = '{season}' AND e.x_legacy IS NOT NULL
),
shooter AS (
  SELECT shooter AS player_id, season_type, {shoot_cols}
  FROM s WHERE shooter IS NOT NULL GROUP BY 1, 2
),
passer AS (
  SELECT passer AS player_id, season_type, {pass_cols}
  FROM s WHERE passer IS NOT NULL GROUP BY 1, 2
)
SELECT COALESCE(sh.player_id, pa.player_id) AS player_id,
       COALESCE(sh.season_type, pa.season_type) AS season_type,
       {out_cols}
FROM shooter sh FULL OUTER JOIN passer pa
  ON pa.player_id = sh.player_id AND pa.season_type = sh.season_type
"""


def assisted_sql(season):
    shoot = ", ".join("SUM(CASE WHEN zone='%s' AND assisted THEN 1 ELSE 0 END) AS %s_fgm_ast"
                      % (z, z) for z in ZONES)
    pas = ", ".join("SUM(CASE WHEN zone='%s' THEN 1 ELSE 0 END) AS %s_ast" % (z, z)
                    for z in ZONES)
    out = ", ".join(["COALESCE(sh.%s_fgm_ast, 0) AS %s_fgm_ast" % (z, z) for z in ZONES]
                    + ["COALESCE(pa.%s_ast, 0) AS %s_ast" % (z, z) for z in ZONES])
    return ASSISTED_SQL.format(zone=ZONE_CASE, season=season, shoot_cols=shoot,
                               pass_cols=pas, out_cols=out)


def cmd_build(targets):
    import duckdb
    env = load_env()
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    with tempfile.TemporaryDirectory() as tmp:
        for s in targets:
            con = connect()
            in_hot = s in hot_seasons(con)
            out = os.path.join(tmp, "ast_%s.parquet" % s)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (RESOLVE_SQL.format(season=s), out))
            stats = dict(con.execute("SELECT match, COUNT(*) FROM read_parquet('%s') GROUP BY 1"
                                     % out).fetchall())
            con.close()

            if in_hot:
                # the hot database is only writable with no other connection open
                rw = duckdb.connect(DB_PATH)
                rw.execute("CREATE TABLE IF NOT EXISTS shot_assists (%s, "
                           "PRIMARY KEY (game_id, action_id))" % AST_COLS)
                rw.execute("DELETE FROM shot_assists WHERE game_id IN "
                           "(SELECT game_id FROM games WHERE season = ?)", [s])
                rw.execute("INSERT INTO shot_assists BY NAME SELECT * FROM read_parquet('%s')" % out)
                rw.commit()
                rw.close()
                where = "hot database"
            else:
                d = os.path.join(CACHE, s)
                os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, "shot_assists.parquet")
                duckdb.connect().execute("COPY (SELECT * FROM read_parquet('%s')) TO '%s' "
                                         "(FORMAT PARQUET, COMPRESSION ZSTD)" % (out, dest))
                s3.upload_file(dest, bucket, "%s/archive/%s/shot_assists.parquet" % (PREFIX, s))
                where = "archive"

            # per-player zone counts for the Stat Database
            con = connect()
            agg = os.path.join(tmp, "assisted_%s.parquet" % s)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (assisted_sql(s), agg))
            n = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % agg).fetchone()[0]
            con.close()
            if n:
                s3.upload_file(agg, bucket, "%s/seasons/%s/assisted.parquet" % (PREFIX, s))
            made = sum(stats.values())
            print("  %-9s %7d made  %5.1f%% assisted  %d resolved  %d ambiguous  "
                  "%d unmatched  -> %s, %d player rows"
                  % (s, made, 100.0 * (made - stats.get("none", 0)) / made if made else 0,
                     stats.get("resolved", 0), stats.get("ambiguous", 0),
                     stats.get("unmatched", 0), where, n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    con = connect()
    targets = seasons(con) if a.all else [a.season] if a.season else None
    if not targets:
        sys.exit("  give --season or --all")
    if a.cmd == "check":
        cmd_check(con, targets)
        return
    con.close()
    cmd_build(targets)


if __name__ == "__main__":
    main()
