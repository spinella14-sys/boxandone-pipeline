#!/usr/bin/env python3
"""
patch_assists.py — carry assists through the player exports and the nightly job.

  export_full.py
    - connect() adds the all_ast view (parse_assists.py's results)
    - a player's shots.parquet gains assisted / assist_player_id / assist_name
    - new players/{id}/assists.parquet: every made shot he assisted, with the
      shooter and the court location — his assist chart
    - `sync` also pulls shot_assists.parquet from the archive

  nightly2.py
    - after play-by-play for new games: parse_assists.py for the season, then
      the per-game files (which now carry the assister)
    - player files refreshed by the nightly job include the assists file
    - after the season export: ingest_tracking.py for the current season

Needs parse_assists.py and ingest_tracking.py in ~/boxandone. Anchors are
checked first; nothing is written if one is missing. Backups: *.bak_assists.

    python3 patch_assists.py
"""
import os
import shutil
import sys

HOME = os.path.expanduser("~/boxandone")

OLD_SHOTS = '''SHOTS_SQL = """
SELECT e.game_id, g.game_date, g.season, g.season_type,
       e.period, e.seconds_left, e.elapsed_seconds, e.team_abbr,
       e.shot_result, e.shot_value, e.shot_distance,
       e.x_legacy, e.y_legacy, e.sub_type AS shot_type, e.description
FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
WHERE e.player_id = '%s' AND e.is_field_goal
ORDER BY g.game_date, e.action_id
"""'''
NEW_SHOTS = '''SHOTS_SQL = """
SELECT e.game_id, g.game_date, g.season, g.season_type,
       e.period, e.seconds_left, e.elapsed_seconds, e.team_abbr,
       e.shot_result, e.shot_value, e.shot_distance,
       e.x_legacy, e.y_legacy, e.sub_type AS shot_type, e.description,
       a.assisted, a.assist_player_id, a.assist_name
FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
LEFT JOIN all_ast a ON a.game_id = e.game_id AND a.action_id = e.action_id
WHERE e.player_id = '%s' AND e.is_field_goal
ORDER BY g.game_date, e.action_id
"""

# every made shot this player assisted — read from the assists table alone,
# which carries the shot's location and shooter
ASSISTS_SQL = """
SELECT a.game_id, g.game_date, g.season, g.season_type, a.period, a.team_abbr,
       a.shooter_id, a.shooter_name, a.shot_value, a.shot_distance,
       a.x_legacy, a.y_legacy, a.shot_type, a.description
FROM all_ast a JOIN all_games g ON g.game_id = a.game_id
WHERE a.assist_player_id = '%s'
ORDER BY g.game_date, a.action_id
"""'''

EDITS = {
    "export_full.py": [
        (OLD_SHOTS, NEW_SHOTS),
        ('NEEDED = ["games.parquet", "player_game_box.parquet", "play_by_play.parquet"]',
         'NEEDED = ["games.parquet", "player_game_box.parquet", "play_by_play.parquet",\n'
         '          "shot_assists.parquet"]'),
        ("""        SELECT * FROM hot.play_by_play
    \"\"\" % glob("play_by_play.parquet"))

    return con
""", """        SELECT * FROM hot.play_by_play
    \"\"\" % glob("play_by_play.parquet"))

    # who assisted each made shot (parse_assists.py); empty until it has run
    sys.path.insert(0, HOME)
    import parse_assists
    parse_assists.add_ast_view(con, CACHE)
    return con
"""),
        ("""            n = put(con, s3, bucket, SHOTS_SQL % pid,
                    "%s/players/%s/shots.parquet" % (PREFIX, pid), tmp)
            if n:
                shots += 1
                size += n
""", """            n = put(con, s3, bucket, SHOTS_SQL % pid,
                    "%s/players/%s/shots.parquet" % (PREFIX, pid), tmp)
            if n:
                shots += 1
                size += n
            n = put(con, s3, bucket, ASSISTS_SQL % pid,
                    "%s/players/%s/assists.parquet" % (PREFIX, pid), tmp)
            if n:
                size += n
"""),
    ],
    "nightly2.py": [
        ("""                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])
                # one play-by-play file per new game, for the game page""",
         """                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])
                # who assisted each new made shot, before anything exports it
                if not a.no_pbp:
                    ok &= run(log, ["parse_assists.py", "build", "--season", season])
                # one play-by-play file per new game, for the game page"""),
        ("""    for view, tbl in (("all_games", "games"), ("all_box", "player_game_box"),
                      ("all_pbp", "play_by_play")):
        con.execute("CREATE VIEW %s AS SELECT * FROM hot.%s" % (view, tbl))

    done = 0""",
         """    for view, tbl in (("all_games", "games"), ("all_box", "player_game_box"),
                      ("all_pbp", "play_by_play")):
        con.execute("CREATE VIEW %s AS SELECT * FROM hot.%s" % (view, tbl))
    import parse_assists
    parse_assists.add_ast_view(con, "/nonexistent")   # hot database only

    done = 0"""),
        ("""        for kind, sql in (("gamelog", X.GAMELOG_SQL), ("shots", X.SHOTS_SQL)):""",
         """        for kind, sql in (("gamelog", X.GAMELOG_SQL), ("shots", X.SHOTS_SQL),
                          ("assists", X.ASSISTS_SQL)):"""),
        ("""            ok &= run(log, ["export_games_index.py", "build", "--season", season])
""", """            ok &= run(log, ["export_games_index.py", "build", "--season", season])
            # NBA.com tracking totals: passing, touches, shots by dribbles
            ok &= run(log, ["ingest_tracking.py", "build", "--season", season])
"""),
    ],
}

MARKERS = {"export_full.py": "ASSISTS_SQL", "nightly2.py": "parse_assists.py"}


def main():
    for f in ("parse_assists.py", "ingest_tracking.py"):
        if not os.path.exists(os.path.join(HOME, f)):
            sys.exit("  NOT PATCHED — %s must be in %s first" % (f, HOME))
    srcs, problems = {}, []
    for rel, edits in EDITS.items():
        path = os.path.join(HOME, rel)
        src = open(path, encoding="utf-8").read()
        if MARKERS[rel] in src:
            problems.append("  %s already patched" % rel)
            continue
        for old, _ in edits:
            n = src.count(old)
            if n != 1:
                problems.append("  %s: anchor found %d time(s), expected 1:\n      %s"
                                % (rel, n, old.strip().splitlines()[0]))
        srcs[rel] = src
    if problems:
        print("  NOT PATCHED — nothing was written:")
        print("\n".join(problems))
        sys.exit(1)
    for rel, src in srcs.items():
        for old, new in EDITS[rel]:
            src = src.replace(old, new)
        path = os.path.join(HOME, rel)
        shutil.copy(path, path + ".bak_assists")
        open(path, "w", encoding="utf-8").write(src)
        print("  patched %s" % rel)


if __name__ == "__main__":
    main()
