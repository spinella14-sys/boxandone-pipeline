#!/usr/bin/env python3
"""
export_creation.py — how much of each player's scoring he made himself.

From the resolved assists (parse_assists.py: every made field goal, back to
1996-97, with whether it was assisted), per player, season and season type:

  fgm_pbp       made field goals, as play-by-play counts them
  fgm_ast       of those, assisted
  fgm_putback   unassisted putbacks and tip-ins — unassisted, but not
                created: an offensive rebound finished, not a shot made
  fg3m_pbp, fg3m_ast   the same for threes

So "self-created makes" = fgm_pbp - fgm_ast - fgm_putback, and the share of
makes he created = that over (fgm_pbp - fgm_putback). The Usage Roles study
(Analytics) and, later, the role labels read it.

    python3 export_creation.py build                    # every season held
    python3 export_creation.py build --seasons 2026-27  # the nightly step

Writes v2/seasons/{season}/creation.parquet.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

SQL = """
SELECT a.shooter_id AS player_id, g.season_type,
       COUNT(*) AS fgm_pbp,
       SUM(CASE WHEN a.assisted THEN 1 ELSE 0 END) AS fgm_ast,
       SUM(CASE WHEN NOT a.assisted AND (lower(coalesce(a.shot_type, '')) LIKE '%putback%'
                                     OR lower(coalesce(a.shot_type, '')) LIKE '%tip%')
                THEN 1 ELSE 0 END) AS fgm_putback,
       SUM(CASE WHEN a.shot_value = 3 THEN 1 ELSE 0 END) AS fg3m_pbp,
       SUM(CASE WHEN a.shot_value = 3 AND a.assisted THEN 1 ELSE 0 END) AS fg3m_ast
FROM all_ast a JOIN all_games g ON g.game_id = a.game_id
WHERE g.season = ? AND a.shooter_id IS NOT NULL
GROUP BY 1, 2
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--seasons")
    a = ap.parse_args()

    import parse_assists as PA
    env = PA.load_env()
    s3, bucket = PA.s3c(env), env["R2_BUCKET_NAME"]
    con = PA.connect()
    held = [s for (s,) in con.execute(
        "SELECT DISTINCT g.season FROM all_ast a JOIN all_games g ON g.game_id = a.game_id ORDER BY 1").fetchall()]
    seasons = a.seasons.split(",") if a.seasons else held
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            if s not in held:
                print("  %s: no resolved assists" % s)
                continue
            out = os.path.join(tmp, "creation_%s.parquet" % s)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (SQL.replace("?", "'%s'" % s), out))
            n, f, pb = con.execute("SELECT COUNT(*), SUM(fgm_pbp), SUM(fgm_putback) FROM read_parquet('%s')"
                                   % out).fetchone()
            s3.upload_file(out, bucket, "v2/seasons/%s/creation.parquet" % s)
            print("  %s: %d player rows, %d makes, %d unassisted putbacks (%.1f%%)"
                  % (s, n, f or 0, pb or 0, 100.0 * (pb or 0) / (f or 1)))


if __name__ == "__main__":
    main()
