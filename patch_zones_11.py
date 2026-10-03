#!/usr/bin/env python3
"""
patch_zones_11.py — eleven shot zones instead of five.

Five was too coarse. These cut along lines that mean something when evaluating
a shooter, and left/right are left and right AS DISPLAYED on the shot chart:
x < 0 renders screen-left, because the chart flips y only.

    ra           restricted area, within 4 ft
    paint        inside the lane, outside the restricted area
    short_mid    4 to 14 ft, outside the lane — the floater and short corner
    long_mid_l   14 ft to the arc, left wing
    long_mid_c   14 ft to the arc, centre
    long_mid_r   14 ft to the arc, right wing
    c3_l         left corner three
    c3_r         right corner three
    atb_l        above the break, left wing
    atb_top      above the break, top of the key
    atb_r        above the break, right wing

Plus a twelfth, `heave`, for anything beyond 30 ft. It exists so half-court
attempts at the buzzer do not drag down above-the-break percentages, which is
the single most common way three-point splits get quietly wrong.

Wings are split by angle from the basket at 60 and 120 degrees rather than by
x, so the boundary follows the arc instead of cutting across it.

shot_value still separates twos from threes, so no assumption is made about
where the line was in a given era.

Run:  python3 patch_zones_11.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

ZONE_CASE = """CASE
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
           END"""

ZONES = ["ra", "paint", "short_mid", "long_mid_l", "long_mid_c", "long_mid_r",
         "c3_l", "c3_r", "atb_l", "atb_top", "atb_r", "heave"]


def build_cte():
    wide = []
    for z in ZONES:
        wide.append("         SUM(CASE WHEN zone='%s' THEN fga ELSE 0 END) AS %s_fga," % (z, z))
        wide.append("         SUM(CASE WHEN zone='%s' THEN fgm ELSE 0 END) AS %s_fgm," % (z, z))
    wide[-1] = wide[-1].rstrip(",")
    return """WITH zones AS (
  SELECT e.player_id, g.season_type,
         %s AS zone,
         COUNT(*) AS fga,
         SUM(CASE WHEN e.shot_result = 'Made' THEN 1 ELSE 0 END) AS fgm
  FROM all_pbp e
  JOIN all_games g ON g.game_id = e.game_id
  WHERE e.is_field_goal AND g.season = '%%s'
    AND e.player_id IS NOT NULL AND e.x_legacy IS NOT NULL
  GROUP BY 1,2,3
),
zone_wide AS (
  SELECT player_id, season_type,
%s
  FROM zones GROUP BY 1,2
)
""" % (ZONE_CASE, "\n".join(wide))


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "atb_top_fga" in src:
        print("  already on eleven zones")
        return

    # strip the five-zone version if it is there
    if "zone_wide" in src:
        start = src.index('SEASON_SQL = """\n') + len('SEASON_SQL = """\n')
        end = src.index("SELECT b.player_id", start)
        src = src[:start] + src[end:]
        for col in ("ra", "paint", "mid", "c3", "atb3"):
            src = src.replace("       z.%s_fga, z.%s_fgm," % (col, col), "")
        import re
        src = re.sub(r"\n *z\.[a-z0-9_]+_fg[am],?", "", src)
        src = src.replace("LEFT JOIN zone_wide z\n  ON z.player_id = b.player_id "
                          "AND z.season_type = g.season_type\n", "")
        src = src.replace("GROUP BY 1,2,3,4,5, \n", "GROUP BY 1,2,3,4,5\n")
        src = re.sub(r"GROUP BY 1,2,3,4,5,[\s\S]*?\n\"\"\"", "GROUP BY 1,2,3,4,5\n\"\"\"", src, count=1)
        src = src.replace("SEASON_SQL % (s, s),", "SEASON_SQL % s,")

    sel = "".join("       z.%s_fga, z.%s_fgm,\n" % (z, z) for z in ZONES)
    grp = ",\n".join("         z.%s_fga, z.%s_fgm" % (z, z) for z in ZONES)

    src = src.replace('SEASON_SQL = """\nSELECT b.player_id',
                      'SEASON_SQL = """\n' + build_cte() + 'SELECT b.player_id')

    anchor = "       MIN(g.game_date) first_game, MAX(g.game_date) last_game\n"
    src = src.replace(anchor, anchor.rstrip("\n") + ",\n" + sel, 1)

    join_anchor = ("JOIN hot.players p ON p.player_id = b.player_id\n"
                   "WHERE b.played AND g.season = '%s'\nGROUP BY 1,2,3,4,5")
    if join_anchor not in src:
        sys.exit("  SEASON_SQL join block does not match expected form")
    src = src.replace(join_anchor,
                      "JOIN hot.players p ON p.player_id = b.player_id\n"
                      "LEFT JOIN zone_wide z\n"
                      "  ON z.player_id = b.player_id AND z.season_type = g.season_type\n"
                      "WHERE b.played AND g.season = '%s'\n"
                      "GROUP BY 1,2,3,4,5,\n" + grp)

    src = src.replace("put(con, s3, bucket, SEASON_SQL % s,",
                      "put(con, s3, bucket, SEASON_SQL % (s, s),")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_z11", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  eleven zones plus a heave bucket, %d columns" % (2 * len(ZONES)))
    for z in ZONES:
        print("    %s" % z)
    print("  backup -> export_full.py.bak_z11")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
