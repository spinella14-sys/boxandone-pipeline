#!/usr/bin/env python3
"""
patch_spinalytics.py — turnover composition from play-by-play.

The box score records one number for turnovers, but play-by-play tags each one,
and the kinds are not equivalent. A stolen pass hands the opponent a running
start; the same pass sailing out of bounds gives them a sideline inbound
against a set defence. Both count as one turnover.

Five buckets, from the sub_type on each turnover event:

    live     Bad Pass, Lost Ball — stolen or poked away, opponent in transition
    oob      the same mistakes going out of bounds, dead ball
    ofoul    offensive fouls, including charges
    viol     travelling, double dribble, shot clock, backcourt, the 3/5/8
             second violations, palming, offensive goaltending
    other    anything the league tags that is none of the above

What that supports:

    BadPass%   passing turnovers as a share of all turnovers — a playmaker's
               mistakes versus a ball-handler's
    Live%      the share that gives up transition, which is the part that
               actually costs points
    LiveTO100  live-ball turnovers per 100 possessions

AST:USG comes free from existing columns: assist rate over usage rate, high for
distributors and low for volume scorers.

Run:  python3 patch_spinalytics.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

# Grouped from the sub_types actually present in the data.
TO_CASE = """CASE
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
           END"""

BUCKETS = ["live", "oob", "ofoul", "viol", "other"]
# which buckets came from a pass rather than a dribble
PASS_TYPES = ("'Bad Pass'", "'Out of Bounds - Bad Pass Turnover'")


def build_cte():
    wide = ["         SUM(CASE WHEN bucket='%s' THEN n ELSE 0 END) AS to_%s," % (b, b)
            for b in BUCKETS]
    wide.append("         SUM(CASE WHEN is_pass THEN n ELSE 0 END) AS to_pass")
    return """tov_types AS (
  SELECT e.player_id, g.season_type,
         %s AS bucket,
         e.sub_type IN (%s) AS is_pass,
         COUNT(*) AS n
  FROM all_pbp e
  JOIN all_games g ON g.game_id = e.game_id
  WHERE lower(e.action_type) LIKE '%%%%turnover%%%%'
    AND g.season = '%%s' AND e.player_id IS NOT NULL
  GROUP BY 1,2,3,4
),
tov_wide AS (
  SELECT player_id, season_type,
%s
  FROM tov_types GROUP BY 1,2
),
""" % (TO_CASE, ", ".join(PASS_TYPES), "\n".join(wide))


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "tov_wide" in src:
        print("  already patched")
        return
    if "zone_wide" not in src:
        sys.exit("  run patch_zones_11.py first")

    # splice the turnover CTEs in ahead of the zones CTE
    src = src.replace('SEASON_SQL = """\nWITH zones AS (',
                      'SEASON_SQL = """\nWITH ' + build_cte() + 'zones AS (')

    cols = "".join("       v.to_%s,\n" % b for b in BUCKETS) + "       v.to_pass,\n"
    grp = ",\n".join("         v.to_%s" % b for b in BUCKETS) + ",\n         v.to_pass"

    anchor = "       z.heave_fga, z.heave_fgm,\n"
    if anchor not in src:
        sys.exit("  could not find the end of the zone column list")
    src = src.replace(anchor, anchor + cols, 1)

    join_anchor = ("LEFT JOIN zone_wide z\n"
                   "  ON z.player_id = b.player_id AND z.season_type = g.season_type\n")
    if join_anchor not in src:
        sys.exit("  could not find the zone join")
    src = src.replace(join_anchor, join_anchor +
                      "LEFT JOIN tov_wide v\n"
                      "  ON v.player_id = b.player_id AND v.season_type = g.season_type\n")

    # extend the GROUP BY
    gb = "         z.heave_fga, z.heave_fgm"
    if gb not in src:
        sys.exit("  could not find the end of the GROUP BY")
    src = src.replace(gb, gb + ",\n" + grp, 1)

    # one more season interpolation, and it comes first in the query text
    src = src.replace("SEASON_SQL % (s, s),", "SEASON_SQL % (s, s, s),")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_spin", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  player_season gains six turnover columns:")
    for b in BUCKETS:
        print("    to_%-6s" % b)
    print("    to_pass   passing turnovers, live or out of bounds")
    print("  backup -> export_full.py.bak_spin")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
