#!/usr/bin/env python3
"""
patch_rosters_season.py — run once.

rosters_export.export_current() also writes v2/rosters/{season}.parquet: every
roster spell of the season from the nightly snapshots, so Game Notes can show
a team's roster as it stood on the day of any game, including players who did
not play.

One row per unbroken run of snapshots a player appeared in for a team:

  season, team_abbr, player_id, full_name, nba_id, position, jersey,
  first_as_of   the first snapshot of the run
  last_as_of    the last snapshot of the run
  until         the first snapshot after the run (he was gone by then);
                NULL while he is still on the roster

He was on the team on day d when first_as_of <= d and (until is NULL or
until > d). A gap between two snapshots counts as still rostered, since the
snapshot simply was not taken that day.

The nightly already calls export_current, so the file stays current. Run
`python3 rosters_export.py` once after this to publish it now.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "rosters_export.py")
src = open(PATH).read()
if "SEASON_SQL" in src:
    print("  rosters_export.py already patched")
    sys.exit()

PAIRS = [
    ('''def export_current(con, s3, bucket):''', '''# every roster spell of a season: islands of consecutive snapshots per (team, player)
SEASON_SQL = """
WITH d AS (
    SELECT as_of, ROW_NUMBER() OVER (ORDER BY as_of) AS i
    FROM (SELECT DISTINCT as_of FROM hot.team_rosters WHERE season = '%(season)s')
),
r AS (
    SELECT t.team_abbr, t.player_id, t.jersey, d.as_of, d.i
    FROM hot.team_rosters t JOIN d ON d.as_of = t.as_of
    WHERE t.season = '%(season)s'
),
isl AS (
    SELECT *, i - ROW_NUMBER() OVER (PARTITION BY team_abbr, player_id ORDER BY i) AS grp FROM r
),
spell AS (
    SELECT team_abbr, player_id, MIN(as_of) AS first_as_of, MAX(as_of) AS last_as_of,
           MAX(i) AS last_i, arg_max(jersey, i) AS jersey
    FROM isl GROUP BY team_abbr, player_id, grp
)
SELECT '%(season)s' AS season, s.team_abbr, s.player_id, p.full_name,
       n.source_id AS nba_id, p.position, s.jersey,
       s.first_as_of, s.last_as_of, nx.as_of AS until
FROM spell s
JOIN hot.players p ON p.player_id = s.player_id
LEFT JOIN hot.player_identifiers n ON n.player_id = s.player_id AND n.source = 'nba'
LEFT JOIN d nx ON nx.i = s.last_i + 1
ORDER BY s.team_abbr, p.full_name, s.first_as_of
"""


def export_season(con, s3, bucket, season):
    """v2/rosters/{season}.parquet — every roster spell of the season."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "season.parquet")
        con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                    % (SEASON_SQL % {"season": season}, p))
        n = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % p).fetchone()[0]
        s3.upload_file(p, bucket, "%s/rosters/%s.parquet" % (PREFIX, season))
    return n


def export_current(con, s3, bucket):''', 1),
    ('''        s3.upload_file(p, bucket, "%s/rosters/current.parquet" % PREFIX)
    return latest[0], n''', '''        s3.upload_file(p, bucket, "%s/rosters/current.parquet" % PREFIX)
    # and the season's spells, for rosters as they stood on any day
    try:
        spells = export_season(con, s3, bucket, latest[1])
        print("  rosters/%s.parquet: %d roster spells" % (latest[1], spells))
    except Exception as e:                       # never let this stop the nightly
        print("  rosters/%s.parquet not written: %s" % (latest[1], str(e)[:120]))
    return latest[0], n''', 1),
]

for old, new, count in PAIRS:
    if src.count(old) != count:
        sys.exit("  rosters_export.py not as expected — nothing written")
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  rosters_export.py patched")
