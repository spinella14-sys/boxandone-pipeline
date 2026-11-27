#!/usr/bin/env python3
"""
patch_lineups_stints.py — run once. The game page's rotation chart.

export_game_lineups.py already reads every stint of a game (a stretch with
the same ten players on the floor) to build the five-man units. It now also
writes those stints, under our player ids, next to the units:

  v2/games/{game_id}/stints.parquet
    side, start_s, end_s     game seconds, 0 at the opening tip
    player_ids               our ids, comma separated
    pts_for, pts_against     the score while that stint lasted, this side's view

The page joins a player's consecutive stints into his shifts and colours each
by its plus-minus. Nothing else changes: same command, same nightly step.

Then rebuild the seasons that have lineups (2019-20 on):
    python3 export_game_lineups.py build --seasons 2019-20:2026-27
"""
import os
import sys

PATH = os.path.join(os.path.expanduser("~/boxandone"), "export_game_lineups.py")
src = open(PATH).read()
if "stints.parquet" in src:
    print("  export_game_lineups.py already patched")
    sys.exit()

PAIRS = [
    # 1. rows for the stints file
    ('''COLS = ["side", "player_ids", "nba_ids",''',
     '''def stint_rows(stints, ids, flip):
    """One row per side per stint, under our ids, for the rotation chart."""
    out = []
    for (gid, per, s0, s1, secs, hids, aids, hp, ap, hpo, apo, rel) in stints:
        for side, mine, pf, pa in (("home", hids, hp, ap), ("away", aids, ap, hp)):
            if flip:
                side = "away" if side == "home" else "home"
            ours = [ids.get(int(x)) or "" for x in (mine or "").split(",") if x]
            out.append({"side": side, "start_s": int(s0 or 0), "end_s": int(s1 or 0),
                        "player_ids": ",".join(ours), "pts_for": int(pf or 0),
                        "pts_against": int(pa or 0)})
    return out


COLS = ["side", "player_ids", "nba_ids",'''),
    # 2. write it beside the units
    ('''def write_one(con_factory, s3, bucket, our_gid, rows):
    con = con_factory()
    with tempfile.TemporaryDirectory() as tmp:''',
     '''def write_one(con_factory, s3, bucket, our_gid, rows, srows=None):
    con = con_factory()
    with tempfile.TemporaryDirectory() as tmp:
        if srows:
            sj = os.path.join(tmp, "s.jsonl")
            with open(sj, "w") as f:
                for r in srows:
                    f.write(json.dumps(r) + "\\n")
            sp = os.path.join(tmp, "s.parquet")
            con.execute("""COPY (SELECT "side"::VARCHAR "side", "start_s"::INTEGER "start_s",
                                        "end_s"::INTEGER "end_s", "player_ids"::VARCHAR "player_ids",
                                        "pts_for"::INTEGER "pts_for", "pts_against"::INTEGER "pts_against"
                                 FROM read_json_auto('%s', format='newline_delimited'))
                           TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (sj, sp))
            if s3:
                s3.upload_file(sp, bucket, "v2/games/%s/stints.parquet" % our_gid,
                               ExtraArgs={"ContentType": "application/octet-stream"})'''),
    ('''            jobs.append((ours, rows))''',
     '''            jobs.append((ours, rows, stint_rows(st, ids, flip)))'''),
    ('''            gid, rows = jobs[0]''',
     '''            gid, rows, _ = jobs[0]'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  export_game_lineups.py not as expected near:\n%s\n  nothing written" % old[:90])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  export_game_lineups.py patched")
print("\n  then: python3 export_game_lineups.py build --seasons 2019-20:2026-27")
