#!/usr/bin/env python3
"""
rosters_export.py — who is on which NBA team today, for the app.

Two things, both read from the dated team_rosters snapshots that
ingest_rosters.py writes every night:

  wrap(con, inner_sql)
      Adds current_team, roster_as_of and roster_status to a registry query.
      roster_status is
        'rostered'     on an NBA roster in the latest snapshot
        'free_agent'   not on one, but on a roster at some point this season
                       or played in the NBA last season or this one
        NULL           anyone else — retired or long gone
      The free-agent rule is deliberately loose: a player who retired this
      summer shows as a free agent until he drops out of the last-season
      window, which is better than silently leaving a recent waive-ee on
      his old team.

  export_current(con, s3, bucket)
      Writes v2/rosters/current.parquet: the latest snapshot, one row per
      rostered player, for the team pages.

`con` must have the working database attached as `hot`.

    python3 rosters_export.py          # export the current roster file now
"""
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PREFIX = "v2"


def _latest(con):
    """(as_of, season) of the newest snapshot, or None when there are none."""
    have = con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name='hot' AND table_name='team_rosters'").fetchone()[0]
    if not have:
        return None
    row = con.execute("""
        SELECT as_of, MAX(season) FROM hot.team_rosters
        WHERE as_of = (SELECT MAX(as_of) FROM hot.team_rosters)
        GROUP BY 1""").fetchone()
    return row if row and row[0] else None


def _prev(season):
    y = int(season[:4])
    return "%d-%s" % (y - 1, str(y)[2:])


def wrap(con, inner):
    """inner: a SELECT producing registry rows with player_id and last_season."""
    latest = _latest(con)
    if not latest:
        return inner
    as_of, season = latest
    return """
SELECT r.*, cr.team_abbr AS current_team, cr.as_of AS roster_as_of,
       CASE WHEN cr.team_abbr IS NOT NULL THEN 'rostered'
            WHEN rs.player_id IS NOT NULL OR r.last_season >= '%(prev)s'
              THEN 'free_agent' END AS roster_status
FROM (%(inner)s) r
LEFT JOIN (SELECT player_id, MIN(team_abbr) AS team_abbr, MAX(as_of) AS as_of
           FROM hot.team_rosters WHERE as_of = DATE '%(as_of)s'
           GROUP BY 1) cr ON cr.player_id = r.player_id
LEFT JOIN (SELECT DISTINCT player_id FROM hot.team_rosters
           WHERE season = '%(season)s') rs ON rs.player_id = r.player_id
""" % {"inner": inner, "as_of": as_of, "season": season, "prev": _prev(season)}


def add_rosters(con, path, tmp):
    """Rewrite a registry parquet file with the roster columns added."""
    sql = wrap(con, "SELECT * FROM read_parquet('%s')" % path)
    out = os.path.join(tmp, "registry_rosters.parquet")
    if os.path.exists(out):
        os.remove(out)
    con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (sql, out))
    return out


CURRENT_SQL = """
SELECT r.as_of, r.season, r.team_abbr, r.player_id, p.full_name,
       n.source_id AS nba_id, p.position, r.jersey, r.position_raw,
       r.height_in, r.weight_lb, r.experience, r.school
FROM hot.team_rosters r
JOIN hot.players p ON p.player_id = r.player_id
LEFT JOIN hot.player_identifiers n ON n.player_id = r.player_id AND n.source = 'nba'
WHERE r.as_of = DATE '%s'
ORDER BY r.team_abbr, p.full_name
"""


def export_current(con, s3, bucket):
    latest = _latest(con)
    if not latest:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "current.parquet")
        con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                    % (CURRENT_SQL % latest[0], p))
        n = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % p).fetchone()[0]
        s3.upload_file(p, bucket, "%s/rosters/current.parquet" % PREFIX)
    return latest[0], n


if __name__ == "__main__":
    import boto3
    import duckdb
    env = {}
    for line in open(os.path.join(HOME, ".env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    r = export_current(con, s3, env["R2_BUCKET_NAME"])
    print("  no roster snapshots" if not r else
          "  rosters/current.parquet: %d players as of %s" % (r[1], r[0]))
