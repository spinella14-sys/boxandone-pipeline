#!/usr/bin/env python3
"""
compute_shotq.py — shot quality and shot-making (xeFG).

Every field goal attempt since 1996-97 is given an expected value: how often
the league makes that kind of shot, from that spot, that season. Summed over a
player's attempts, that separates two things eFG% mixes together:

  shot quality   xeFG% — the eFG% his shot diet would yield for an average
                 shooter. High: rim attempts, open-looking threes, putbacks.
  shot-making    eFG% minus xeFG% — how much better or worse than that he made
                 them. The "taking the right shots, they just aren't going in"
                 question, in one number.

A shot's kind is its zone and its type, both from the play-by-play:

  zone   restricted area (0-4 ft), paint (4-8), short mid (8-14), long mid
         (14 ft to the line), corner three, above-the-break three, and heaves
         (30+ ft with under 3 seconds left in the period, kept apart so
         buzzer-beaters don't count against anyone)
  type   dunk, alley-oop, putback/tip, layup, hook, floater, pull-up,
         step-back, fadeaway/turnaround, other jumper

Each season's league make rate for every zone x type, regular season, shrunk
toward the zone's own rate where a combination is rare (50 attempts of
weight). The defender's distance is not in the play-by-play, so it is not part
of the shot's kind; whether a shot was assisted is known only for makes, so it
can't be either.

    python3 compute_shotq.py build                    # every season
    python3 compute_shotq.py build --seasons 2026-27  # the nightly step

Writes v2/models/shotq.parquet: player_id, season, season_type, fga, efg,
xefg, making (eFG - xeFG), quality (xeFG - league eFG), pts_making (points
added by shot-making), and the same split for rim, mid-range and threes.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

SHRINK = 50.0

SHOTS = """
CREATE OR REPLACE TEMP TABLE shots AS
SELECT e.player_id, g.season, g.season_type,
       CASE WHEN e.shot_result = 'Made' THEN 1 ELSE 0 END AS made,
       COALESCE(e.shot_value, 2) AS val,
       CASE
         WHEN e.shot_distance >= 30 AND e.seconds_left < 3 THEN 'heave'
         WHEN COALESCE(e.shot_value, 2) = 3 AND COALESCE(e.y_legacy, 999) <= 92 THEN 'c3'
         WHEN COALESCE(e.shot_value, 2) = 3 THEN 'atb3'
         WHEN e.shot_distance < 4 THEN 'ra'
         WHEN e.shot_distance < 8 THEN 'paint'
         WHEN e.shot_distance < 14 THEN 'smid'
         ELSE 'lmid' END AS zone,
       CASE
         WHEN lower(e.sub_type) LIKE '%alley%' THEN 'alley'
         WHEN lower(e.sub_type) LIKE '%putback%' OR lower(e.sub_type) LIKE '%tip%' THEN 'putback'
         WHEN lower(e.sub_type) LIKE '%dunk%' THEN 'dunk'
         WHEN lower(e.sub_type) LIKE '%hook%' THEN 'hook'
         WHEN lower(e.sub_type) LIKE '%layup%' OR lower(e.sub_type) LIKE '%finger roll%' THEN 'layup'
         WHEN lower(e.sub_type) LIKE '%float%' THEN 'floater'
         WHEN lower(e.sub_type) LIKE '%step back%' OR lower(e.sub_type) LIKE '%stepback%' THEN 'stepback'
         WHEN lower(e.sub_type) LIKE '%pull%' THEN 'pullup'
         WHEN lower(e.sub_type) LIKE '%fade%' OR lower(e.sub_type) LIKE '%turnaround%' THEN 'fade'
         ELSE 'jumper' END AS kind
FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
WHERE e.is_field_goal AND e.player_id IS NOT NULL AND g.season = ?
"""

EXPECT = """
CREATE OR REPLACE TEMP TABLE xp AS
WITH reg AS (SELECT * FROM shots WHERE season_type = 'regular'),
     z AS (SELECT zone, AVG(made) AS pz FROM reg GROUP BY 1),
     zk AS (SELECT zone, kind, SUM(made) AS m, COUNT(*) AS n FROM reg GROUP BY 1, 2)
SELECT zk.zone, zk.kind, (zk.m + %f * z.pz) / (zk.n + %f) AS p
FROM zk JOIN z USING (zone)
""" % (SHRINK, SHRINK)

PLAYER = """
WITH s AS (
  SELECT s.*, COALESCE(x.p, z.pz) AS p,
         CASE WHEN s.zone IN ('ra', 'paint') THEN 'rim'
              WHEN s.zone IN ('smid', 'lmid') THEN 'mid'
              WHEN s.zone IN ('c3', 'atb3') THEN 'three' ELSE 'heave' END AS band
  FROM shots s LEFT JOIN xp x USING (zone, kind)
  LEFT JOIN (SELECT zone, AVG(made) AS pz FROM shots WHERE season_type = 'regular' GROUP BY 1) z USING (zone)
  WHERE s.zone <> 'heave'
),
lg AS (SELECT SUM(made * val) / (2.0 * COUNT(*)) AS lg_efg FROM s WHERE season_type = 'regular')
SELECT player_id, season, season_type,
       COUNT(*) AS fga,
       SUM(made * val) / (2.0 * COUNT(*)) AS efg,
       SUM(p * val) / (2.0 * COUNT(*)) AS xefg,
       SUM(made * val) / (2.0 * COUNT(*)) - SUM(p * val) / (2.0 * COUNT(*)) AS making,
       SUM(p * val) / (2.0 * COUNT(*)) - ANY_VALUE(lg.lg_efg) AS quality,
       SUM(made * val) - SUM(p * val) AS pts_making,
       %s
FROM s, lg GROUP BY 1, 2, 3
""" % ",\n       ".join(
    "SUM(CASE WHEN band = '{b}' THEN 1 ELSE 0 END) AS {b}_fga, "
    "SUM(CASE WHEN band = '{b}' THEN (made - p) * val END) / NULLIF(2.0 * SUM(CASE WHEN band = '{b}' THEN 1 ELSE 0 END), 0) "
    "AS {b}_making".format(b=b) for b in ("rim", "mid", "three"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--seasons")
    a = ap.parse_args()
    import duckdb
    import parse_assists as PA
    env = PA.load_env()
    s3, bucket = PA.s3c(env), env["R2_BUCKET_NAME"]
    con = PA.connect()
    held = [s for (s,) in con.execute("SELECT DISTINCT season FROM all_games ORDER BY 1").fetchall()]
    seasons = a.seasons.split(",") if a.seasons else held
    with tempfile.TemporaryDirectory() as tmp:
        # the combined file is kept whole: seasons not rebuilt keep their rows
        allp = os.path.join(tmp, "shotq.parquet")
        old = []
        try:
            s3.download_file(bucket, "v2/models/shotq.parquet", allp)
            old = [allp]
        except Exception:
            pass
        parts = []
        for s in seasons:
            if s not in held:
                print("  %s: no games" % s)
                continue
            con.execute(SHOTS, [s])
            con.execute(EXPECT)
            p = os.path.join(tmp, "q_%s.parquet" % s)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (PLAYER, p))
            n, lg = con.execute("SELECT COUNT(*), SUM(fga) FROM read_parquet('%s')" % p).fetchone()
            spread = con.execute("""SELECT quantile_cont(making, 0.1), quantile_cont(making, 0.9)
                                    FROM read_parquet('%s') WHERE season_type = 'regular' AND fga >= 500""" % p).fetchone()
            print("  %s: %d player rows, %d shots; shot-making, 10th to 90th percentile (500+ FGA): %+.1f to %+.1f eFG pts"
                  % (s, n, lg or 0, 100 * (spread[0] or 0), 100 * (spread[1] or 0)))
            parts.append(p)
        if not parts:
            return
        out = os.path.join(tmp, "out.parquet")
        keep = ("SELECT * FROM read_parquet('%s') WHERE season NOT IN (SELECT UNNEST(?::VARCHAR[]))" % allp) if old else None
        sel = "SELECT * FROM read_parquet(%s, union_by_name=true)" % repr(parts)
        duckdb.connect().execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                                 % (sel + (" UNION ALL BY NAME " + keep if keep else ""), out),
                                 [seasons] if keep else [])
        s3.upload_file(out, bucket, "v2/models/shotq.parquet")
        print("  v2/models/shotq.parquet written")


if __name__ == "__main__":
    main()
