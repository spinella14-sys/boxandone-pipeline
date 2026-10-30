#!/usr/bin/env python3
"""
export_team_shots.py — one shot file per team and season, both ends.

  v2/teams/{abbr}/shots/{season}.parquet

Every field goal with a location in that team's games that season, the
team's own and its opponents', so the team page's shot chart can show its
offence and its defence:

  game_id, game_date, season_type, period, player_id, player_name,
  team_abbr (the shooter's team), opp_abbr, defense (true = an opponent's shot),
  shot_result, shot_value, shot_distance, x_legacy, y_legacy, shot_type, assisted

Team codes are NBA.com's (PHX, BKN, CHA), the spelling the site's canonAbbr()
uses, so a page reached as PHO still finds PHX's file.

    python3 export_team_shots.py build --seasons 1996-97:2026-27   # everything, once
    python3 export_team_shots.py build --season 2026-27 --hot      # the nightly
    python3 export_team_shots.py check --season 2024-25            # counts, writes nothing

--hot reads the working database alone (the nightly never downloads the
archive); without it, archived seasons are read from data/archive/.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
PREFIX = "v2"

CANON = "CASE {c} WHEN 'PHO' THEN 'PHX' WHEN 'BRK' THEN 'BKN' WHEN 'CHO' THEN 'CHA' ELSE {c} END"

SHOTS_SQL = """
CREATE OR REPLACE TEMP TABLE season_shots AS
WITH fg AS (
    SELECT e.game_id, g.game_date, g.season_type, e.period, e.player_id, e.player_name,
           %(team)s AS team_abbr, e.shot_result, e.shot_value, e.shot_distance,
           e.x_legacy, e.y_legacy, e.sub_type AS shot_type, a.assisted, e.action_id
    FROM all_pbp e
    JOIN all_games g ON g.game_id = e.game_id
    LEFT JOIN all_ast a ON a.game_id = e.game_id AND a.action_id = e.action_id
    WHERE g.season = '%(season)s' AND e.is_field_goal AND e.x_legacy IS NOT NULL
      AND e.team_abbr IS NOT NULL
),
sides AS (
    -- the two teams in each game, from its own shots
    SELECT game_id, list(DISTINCT team_abbr) AS t FROM fg GROUP BY 1
)
SELECT fg.*, CASE WHEN s.t[1] = fg.team_abbr THEN s.t[2] ELSE s.t[1] END AS opp_abbr
FROM fg JOIN sides s USING (game_id)
WHERE len(s.t) = 2
"""

TEAM_SQL = """
SELECT game_id, game_date, season_type, period, player_id, player_name, team_abbr, opp_abbr,
       team_abbr <> '%(abbr)s' AS defense, shot_result, shot_value, shot_distance,
       x_legacy, y_legacy, shot_type, assisted
FROM season_shots
WHERE team_abbr = '%(abbr)s' OR opp_abbr = '%(abbr)s'
ORDER BY game_date, game_id, action_id
"""


def season_label(y):
    return "%d-%s" % (y, str(y + 1)[2:])


def seasons_from(spec):
    lo, hi = spec.split(":") if ":" in spec else (spec, spec)
    return [season_label(y) for y in range(int(lo[:4]), int(hi[:4]) + 1)]


def connect(hot_only):
    import duckdb
    import parse_assists
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    have_cache = not hot_only and os.path.isdir(CACHE) and os.listdir(CACHE)
    for view, tbl, f in (("all_games", "games", "games.parquet"),
                         ("all_pbp", "play_by_play", "play_by_play.parquet")):
        if have_cache:
            con.execute("""CREATE VIEW %s AS SELECT * FROM read_parquet('%s', union_by_name=true)
                           UNION ALL BY NAME SELECT * FROM hot.%s"""
                        % (view, os.path.join(CACHE, "*", f), tbl))
        else:
            con.execute("CREATE VIEW %s AS SELECT * FROM hot.%s" % (view, tbl))
    parse_assists.add_ast_view(con, CACHE if have_cache else "/nonexistent")
    return con


def run(seasons, hot_only, upload):
    con = connect(hot_only)
    s3 = bucket = None
    if upload:
        import nightly2 as N
        env = N.load_env()
        s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    with tempfile.TemporaryDirectory() as tmp:
        for season in seasons:
            con.execute(SHOTS_SQL % {"season": season, "team": CANON.format(c="e.team_abbr")})
            n = con.execute("SELECT COUNT(*) FROM season_shots").fetchone()[0]
            if not n:
                print("  %s: no shots with locations" % season)
                continue
            teams = [r[0] for r in con.execute(
                "SELECT DISTINCT team_abbr FROM season_shots ORDER BY 1").fetchall()]
            size = 0
            for abbr in teams:
                if upload:
                    p = os.path.join(tmp, "t.parquet")
                    if os.path.exists(p):
                        os.remove(p)
                    con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                                % (TEAM_SQL % {"abbr": abbr}, p))
                    size += os.path.getsize(p)
                    s3.upload_file(p, bucket, "%s/teams/%s/shots/%s.parquet" % (PREFIX, abbr, season),
                                   ExtraArgs={"ContentType": "application/octet-stream"})
            per = con.execute("""SELECT team_abbr, COUNT(*) FROM season_shots GROUP BY 1
                                 ORDER BY 2 DESC LIMIT 1""").fetchone()
            print("  %s: %d shots, %d teams%s · most %s %d"
                  % (season, n, len(teams), " · %.1f MB written" % (size / 1048576.0) if upload else "",
                     per[0], per[1]))
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "check"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    ap.add_argument("--hot", action="store_true", help="the working database only (nightly)")
    a = ap.parse_args()
    spec = a.seasons or a.season
    if not spec:
        sys.exit("  --season or --seasons")
    run(seasons_from(spec), a.hot, a.cmd == "build")


if __name__ == "__main__":
    main()
