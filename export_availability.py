#!/usr/bin/env python3
"""
export_availability.py — MIN%, active games, and who qualifies.

MIN% is the share of the minutes available in the games a player was ACTIVE
for: minutes played / (48 + 5 per overtime, summed over his active games).

Active means he played, or he sat with a coach's-decision DNP. Ruled out means:
  - the box score gives a reason other than a coach's decision (Did Not Dress,
    Not With Team, Suspended, or — in the NBA.com feed — the injury itself)
  - he is on the inactive list (absent from the box score: never counted)
  - the final injury report before the game had him OUT, which overrides a
    box score's plain "Did Not Play" (2019-20 on, where we hold the reports).
    Basketball Reference files some injured players as "Did Not Play"; the
    report catches them. Before 2019-20 the box score's word stands.
A player who played is active, whatever the report said.

The qualifying rule, the site-wide pool for every ranking, heatmap and model
consensus:
    MIN% >= 15  AND  (active in >= 25% of his team's games  OR  300+ minutes)
The minutes clause keeps a high-minute player who got hurt in the pool.
"His team's games" is the most games any team he appeared for played in that
season type, so it grows through the season and fits the playoffs.

    python3 export_availability.py                       # current season (nightly)
    python3 export_availability.py --seasons all         # every season
    python3 export_availability.py --seasons 2024-25,2025-26

Writes v2/seasons/{season}/availability.parquet: player_id, season,
season_type, gp, active_g, out_g, team_g, mp, avail_min, min_pct, qual.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import parse_assists as PA  # noqa: E402

MIN_PCT = 15.0
GAME_SHARE = 0.25
MIN_MINUTES = 300

SQL = """
WITH inj AS (
  SELECT DISTINCT player_id, game_date FROM {inj}
  WHERE player_id IS NOT NULL AND upper(status) = 'OUT'
),
box AS (
  SELECT b.player_id, b.game_id, b.team_abbr, g.season, g.season_type,
         COALESCE(b.seconds_played, 0) AS secs,
         48 + 5 * COALESCE(g.overtimes, 0) AS glen,
         CASE WHEN b.played THEN 'played'
              WHEN i.player_id IS NOT NULL THEN 'out'
              WHEN lower(trim(COALESCE(b.dnp_reason, 'did not play'))) IN ('did not play', 'dnp')
                   OR lower(b.dnp_reason) LIKE '%coach%' THEN 'coach'
              ELSE 'out' END AS cls
  FROM all_box b JOIN all_games g ON g.game_id = b.game_id
  LEFT JOIN inj i ON i.player_id = b.player_id AND i.game_date = g.game_date
  WHERE g.season = ? AND upper(COALESCE(g.league, 'NBA')) = 'NBA'
),
tg AS (SELECT team_abbr, season_type, COUNT(DISTINCT game_id) AS n FROM box GROUP BY 1, 2),
p AS (
  SELECT b.player_id, b.season, b.season_type,
         COUNT(*) FILTER (WHERE cls = 'played') AS gp,
         COUNT(*) FILTER (WHERE cls <> 'out') AS active_g,
         COUNT(*) FILTER (WHERE cls = 'out') AS out_g,
         MAX(tg.n) AS team_g,
         SUM(secs) / 60.0 AS mp,
         SUM(glen) FILTER (WHERE cls <> 'out') AS avail_min
  FROM box b JOIN tg USING (team_abbr, season_type)
  GROUP BY 1, 2, 3
)
SELECT *, 100.0 * mp / NULLIF(avail_min, 0) AS min_pct,
       COALESCE(100.0 * mp / NULLIF(avail_min, 0) >= {mp_line}
                AND (active_g >= {share} * team_g OR mp >= {mins}), FALSE) AS qual
FROM p
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons")
    a = ap.parse_args()
    env = PA.load_env()
    s3, bucket = PA.s3c(env), env["R2_BUCKET_NAME"]
    con = PA.connect()
    held = [s for (s,) in con.execute("SELECT DISTINCT season FROM all_games ORDER BY 1").fetchall()]
    if not a.seasons:
        seasons = held[-1:]
    elif a.seasons == "all":
        seasons = held
    else:
        seasons = a.seasons.split(",")
    try:
        con.execute("SELECT 1 FROM hot.injury_rows LIMIT 1")
        inj = "hot.injury_rows"
    except Exception:
        inj = "(SELECT NULL::VARCHAR AS player_id, NULL::DATE AS game_date, NULL::VARCHAR AS status WHERE FALSE)"
        print("  no injury reports held — box score reasons only")
    sql = SQL.format(inj=inj, mp_line=MIN_PCT, share=GAME_SHARE, mins=MIN_MINUTES)
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            if s not in held:
                print("  %s: no games" % s)
                continue
            p = os.path.join(tmp, "avail_%s.parquet" % s)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (sql.replace("?", "'%s'" % s), p))
            n, q, med = con.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE qual),
                                              median(min_pct) FILTER (WHERE qual)
                                       FROM read_parquet('%s') WHERE season_type = 'regular'""" % p).fetchone()
            s3.upload_file(p, bucket, "v2/seasons/%s/availability.parquet" % s)
            print("  %s: %d regular-season players, %d qualify (median MIN%% %.1f)" % (s, n, q, med or 0))


if __name__ == "__main__":
    main()
