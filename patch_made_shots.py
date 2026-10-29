#!/usr/bin/env python3
"""
patch_made_shots.py — run once.

In 69 games of 2019-20 NBA's live feed writes shotResult as ": Made" /
": Missed" rather than "Made" / "Missed", and free-throw sub-types as "1of2"
rather than "1 of 2". Anything testing shotResult == "Made" counts every one of
those shots as a miss:

  build_lineups.py   the per-stint tallies: FGM, 3PM, FTM and AST were 0 in
                     those games (points were right — they come from the score)
  cdn_feed.py        the translation to playbyplayv3's shape, used when
                     play-by-play is ingested from the CDN (--source cdn)

Both now read the result as made/missed whatever the punctuation, and the
free-throw sub-type is spaced the usual way.

It then checks the play-by-play we hold (the archive cache and the hot
database) for games where 40+ field goals were taken and none went in — the
mark this bug would leave if any were ingested from the CDN. Read-only.
"""
import glob
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected (%r) — nothing written" % (name, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


patch("build_lineups.py", [
    ('''    made = (a.get("shotResult") or "") == "Made"''',
     '''    # 2019-20 writes ": Made" in some games
    made = (a.get("shotResult") or "").strip(" :").lower() == "made"'''),
], '''strip(" :").lower() == "made"''')

patch("cdn_feed.py", [
    ('''        res = a.get("shotResult") or ""''',
     '''        res = (a.get("shotResult") or "").strip(" :")       # 2019-20 writes ": Made" in some games
        res = {"made": "Made", "missed": "Missed"}.get(res.lower(), res)
        st = re.sub(r"^(\\d+)\\s*of\\s*(\\d+)$", r"\\1 of \\2", st) if t == "freethrow" else st'''),
], '''.strip(" :")       # 2019-20''')

# ---------------------------------------------------------------- did any reach our play-by-play?
import duckdb  # noqa: E402

con = duckdb.connect()
sources = sorted(glob.glob(os.path.join(HOME, "data", "archive", "*", "play_by_play.parquet")))
db = os.path.join(HOME, "data", "boxandone.duckdb")
q = """SELECT game_id, COUNT(*) FILTER (WHERE is_field_goal) fga,
              COUNT(*) FILTER (WHERE is_field_goal AND shot_result = 'Made') fgm
       FROM {src} GROUP BY 1 HAVING fga >= 40 AND fgm = 0"""
bad = []
if sources:
    bad += con.execute(q.format(src="read_parquet(%r, union_by_name=true)" % sources)).fetchall()
if os.path.exists(db):
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % db)
    bad += con.execute(q.format(src="hot.play_by_play")).fetchall()
print("\n  play-by-play checked: %d archived seasons%s" % (len(sources), " + the hot database" if os.path.exists(db) else ""))
if bad:
    print("  %d games have 40+ field goals and no makes — re-ingest them:" % len(bad))
    print("    " + ", ".join(str(g[0]) for g in bad[:40]) + (" …" if len(bad) > 40 else ""))
else:
    print("  no game with field goals and zero makes — the shot charts and zones are unaffected")
