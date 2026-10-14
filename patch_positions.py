#!/usr/bin/env python3
"""
patch_positions.py — run once, before assign_positions.py apply.

The registry export (nightly2.py and export_full.py) now reads the
player_positions table: an admin-set position on the player record wins,
otherwise the automatic one is used. Two new columns tell the site which it
is: position_source ('admin' or 'auto_v1') and position_confidence.
Until player_positions exists the registry is built exactly as before.
"""
import os
import sys

HOME = os.path.expanduser("~/boxandone")

# ---------------------------------------------------------------- nightly2
path = os.path.join(HOME, "nightly2.py")
src = open(path).read()
if "player_positions" in src:
    print("  nightly2.py     already patched")
else:
    anchor = '        sql = ("""\n            COPY (\n              SELECT p.player_id, p.full_name'
    if src.count(anchor) != 1:
        sys.exit("  nightly2.py: registry query not found as expected — nothing written")
    add = '''        # positions: an admin value on the player record wins, else the
        # automatic first pass (assign_positions.py), flagged as such
        has_pp = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() "
            "WHERE database_name='hot' AND table_name='player_positions'").fetchone()[0] > 0
        if has_pp:
            cols = cols.replace("p.position,", """COALESCE(p.position, pp.position) AS position,
                CASE WHEN p.position IS NOT NULL THEN 'admin' ELSE pp.source END AS position_source,
                CASE WHEN p.position IS NOT NULL THEN 1.0 ELSE pp.confidence END AS position_confidence,""")
            join = join + " LEFT JOIN hot.player_positions pp ON pp.player_id=p.player_id"
'''
    src = src.replace(anchor, add + anchor)
    open(path, "w").write(src)
    print("  nightly2.py     patched")

# ------------------------------------------------------------- export_full
path = os.path.join(HOME, "export_full.py")
src = open(path).read()
if "player_positions" in src:
    print("  export_full.py  already patched")
else:
    old_col = "       p.position,\n       COALESCE(p.height_in, bio.height_in) AS height_in,"
    new_col = ("       COALESCE(p.position, pp.position) AS position,\n"
               "       CASE WHEN p.position IS NOT NULL THEN 'admin' ELSE pp.source END AS position_source,\n"
               "       CASE WHEN p.position IS NOT NULL THEN 1.0 ELSE pp.confidence END AS position_confidence,\n"
               "       COALESCE(p.height_in, bio.height_in) AS height_in,")
    old_join = "LEFT JOIN hot.player_bio bio ON bio.player_id=p.player_id AND bio.source='nba'\n"
    new_join = old_join + "LEFT JOIN hot.player_positions pp ON pp.player_id=p.player_id\n"
    if src.count(old_col) != 1 or src.count(old_join) != 1:
        sys.exit("  export_full.py: registry query not found as expected — nothing written")
    src = src.replace(old_col, new_col).replace(old_join, new_join)
    open(path, "w").write(src)
    print("  export_full.py  patched (its registry export now needs player_positions,"
          " which assign_positions.py apply creates)")
