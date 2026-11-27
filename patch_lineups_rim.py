#!/usr/bin/env python3
"""
patch_lineups_rim.py — run once.

  1. Each lineup stint also counts each side's shots at the rim (the
     restricted area, as the shot chart's Rim zone): rim_fga and rim_fgm.
     That gives "opponent rim rate" and "opponent rim FG%" with a player on
     the floor and off it, and the team's own rim rate.
  2. A guard: build_lineups.py won't upload a season with fewer games than the
     copy already on R2 (a machine missing part of the play-by-play cache
     would otherwise replace a full season with a partial one). It says so and
     keeps R2's copy.

After this, rebuild every season's stints once (see the commands Claude gave),
then build_onoff.py.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "build_lineups.py")
src = open(PATH).read()
if '"rim_fga"' in src:
    print("  build_lineups.py already patched")
    sys.exit()

PAIRS = [
    ('''FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")''',
     '''FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk",
           "rim_fga", "rim_fgm")


def at_rim(a):
    """A shot from the restricted area — the shot chart's Rim zone. The feed
    names the area; where it doesn't, four feet or closer."""
    area = (a.get("area") or "").lower()
    if area:
        return area.startswith("restricted")
    try:
        return float(a.get("shotDistance")) <= 4
    except (TypeError, ValueError):
        return False'''),
    ('''        if t == "3pt":
            mine["fg3a"] += 1
            mine["fg3m"] += made
''', '''        if t == "3pt":
            mine["fg3a"] += 1
            mine["fg3m"] += made
        elif at_rim(a):
            mine["rim_fga"] += 1
            mine["rim_fgm"] += made
'''),
    ('''        for name, (rows, cols) in files.items():
            path = os.path.join(OUT, name)''',
     '''        if s3 and not lineups_complete(s3, bucket, con, season, len(vals)):
            continue
        for name, (rows, cols) in files.items():
            path = os.path.join(OUT, name)'''),
    ('''def cmd_build(seasons, upload=True):''',
     '''def lineups_complete(s3, bucket, con, season, n):
    """False (and says so) when R2 already holds this season with more games
    than this build found: this machine is missing play-by-play, and a partial
    season must not replace a full one."""
    tmp = os.path.join(tempfile.gettempdir(), "r2_%s_games.parquet" % season)
    try:
        s3.download_file(bucket, "v2/lineups/%s_games.parquet" % season, tmp)
        have = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [tmp]).fetchone()[0]
    except Exception:
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if n < have:
        print("      NOT uploaded: this build found %d games, R2 has %d — R2's copy kept" % (n, have))
        return False
    return True


def cmd_build(seasons, upload=True):'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  build_lineups.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  build_lineups.py patched: rim attempts counted per stint; partial seasons aren't uploaded")
