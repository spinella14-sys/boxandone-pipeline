#!/usr/bin/env python3
"""
patch_team_export.py — add opponent totals and team minutes to team_season.

The export has each team's own totals but nothing about what they allowed.
That blocks a whole category of stats, because the denominators live on the
other side of the ball:

    ORB%  = ORB / (ORB + opponent DRB)
    DRB%  = DRB / (DRB + opponent ORB)
    BLK%  = blocks per opponent two-point attempt
    DRtg  = opponent points per 100 opponent possessions
    the defensive half of the four factors

player_game_box already carries opp_abbr on every row, so the opponent line is
one more aggregation over the same table — summing rows where a team appears
as the OPPONENT rather than as the team.

Also adds team_mp. Every standard per-player rate (AST%, ORB%, STL%, USG%)
needs team minutes to scale a player's share of time on the floor; the export
had no way to supply it.

Run:  python3 patch_team_export.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

NEW_TEAM_SQL = '''TEAM_SQL = """
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
        + (p.opp_fga + 0.44*p.opp_fta - p.opp_orb + p.opp_tov)) / 2.0 AS poss_est
FROM own o
JOIN opp p
  ON p.season = o.season AND p.league = o.league
 AND p.season_type = o.season_type AND p.team_abbr = o.team_abbr
"""'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "opp_poss_raw" in src:
        print("  already patched")
        return

    start = src.find('TEAM_SQL = """')
    if start == -1:
        sys.exit("  TEAM_SQL not found")
    end = src.find('"""', start + 14) + 3
    src = src[:start] + NEW_TEAM_SQL + src[end:]

    # the query now takes the season twice
    src = src.replace('TEAM_SQL % s,', 'TEAM_SQL % (s, s),')

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_team", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  team_season now carries:")
    print("    team_mp        total team minutes (denominator for AST%, ORB%, USG%)")
    print("    opp_*          15 opponent counting columns")
    print("    poss_raw       own possession estimate")
    print("    opp_poss_raw   opponent possession estimate")
    print("    poss_est       average of the two (the convention)")
    print("  backup -> export_full.py.bak_team")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
