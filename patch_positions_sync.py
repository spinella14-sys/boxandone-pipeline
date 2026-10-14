#!/usr/bin/env python3
"""
patch_positions_sync.py — run once. The nightly and hourly jobs now pull
admin positions from Supabase (positions_supabase.py pull) before the
registry is rebuilt, and the hourly job counts a changed position as a
change worth pushing. Needs positions_supabase.py in ~/boxandone.
"""
import os
import sys

HOME = os.path.expanduser("~/boxandone")


def patch(name, pairs):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if "positions_supabase.py" in src:
        print("  %-11s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s: expected text not found once — nothing written to it" % name)
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %-11s patched" % name)


patch("nightly2.py", [(
    '        try:\n            log("rebuilding registry and manifest")\n',
    '        # positions ruled in the site\'s Positions tool, before the registry\n'
    '        ok &= run(log, ["positions_supabase.py", "pull"])\n'
    '        try:\n            log("rebuilding registry and manifest")\n')])

patch("hourly.py", [
    ('        ok &= N.run(log, ["sync_players_supabase.py"])\n',
     '        ok &= N.run(log, ["sync_players_supabase.py"])\n'
     '        # positions ruled in the site\'s Positions tool\n'
     '        ok &= N.run(log, ["positions_supabase.py", "pull"])\n'),
    ('           ORDER BY 1, 2""",\n',
     '           ORDER BY 1, 2""",\n'
     '        """SELECT player_id, position FROM player_positions\n'
     '           WHERE source = \'admin\' AND ? IS NOT NULL ORDER BY 1""",\n'),
])
