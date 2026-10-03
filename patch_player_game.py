#!/usr/bin/env python3
"""
patch_player_game.py — export every player-game row per season.

The season files are pre-aggregated, so a date filter cannot reach inside them.
This adds the game-grain file that makes date ranges possible:

    v2/seasons/{season}/player_game.parquet

One file, three uses:

  date ranges   "before Feb 1", "March only", "since the deadline" — just a
                filter and a re-sum, which is the payoff of having ingested at
                game grain rather than season grain
  roster        every team a player appeared for, so a traded player shows on
                both rather than being collapsed by MAX(team_abbr)
  splits        home/road, by opponent, before/after any date

About 1 MB per season against 30 KB for the aggregate, so the app loads the
aggregate by default and fetches this lazily the first time a date filter is
touched.

full_name is denormalised in so the client needs no registry join; Parquet
dictionary-encodes the repeats, so it costs almost nothing.

Run:  python3 patch_player_game.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

PG_SQL = '''
PLAYER_GAME_SQL = """
SELECT b.player_id, p.full_name,
       b.game_id, g.game_date, g.season, g.league, g.season_type,
       b.team_abbr, b.opp_abbr, b.is_home, b.started,
       b.seconds_played/60.0 AS mp,
       b.fgm, b.fga, b.fg3m, b.fg3a, b.ftm, b.fta,
       b.orb, b.drb, b.trb, b.ast, b.stl, b.blk, b.tov, b.pf, b.pts,
       b.plus_minus,
       date_diff('day', p.birthdate, g.game_date)/365.25 AS age
FROM all_box b
JOIN all_games g ON g.game_id = b.game_id
JOIN hot.players p ON p.player_id = b.player_id
WHERE b.played AND g.season = '%s'
ORDER BY g.game_date, b.team_abbr, b.player_id
"""

'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "PLAYER_GAME_SQL" in src:
        print("  already patched")
        return

    anchor = "GAMELOG_SQL = \"\"\""
    if anchor not in src:
        sys.exit("  could not find GAMELOG_SQL to anchor against")
    src = src.replace(anchor, PG_SQL.lstrip("\n") + anchor, 1)

    old = '''            b = put(con, s3, bucket, TEAM_SQL % (s, s, s, s),
                    "%s/seasons/%s/team_season.parquet" % (PREFIX, s), tmp)
            tot += (a or 0) + (b or 0)
            print("  %-9s %6.1f KB" % (s, ((a or 0) + (b or 0)) / 1024.0))'''
    new = '''            b = put(con, s3, bucket, TEAM_SQL % (s, s, s, s),
                    "%s/seasons/%s/team_season.parquet" % (PREFIX, s), tmp)
            c = put(con, s3, bucket, PLAYER_GAME_SQL % s,
                    "%s/seasons/%s/player_game.parquet" % (PREFIX, s), tmp)
            tot += (a or 0) + (b or 0) + (c or 0)
            print("  %-9s agg %5.0f KB   team %4.0f KB   games %6.0f KB"
                  % (s, (a or 0) / 1024.0, (b or 0) / 1024.0, (c or 0) / 1024.0))'''
    if old not in src:
        sys.exit("  cmd_seasons body does not match expected form")
    src = src.replace(old, new)

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_pg", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  cmd_seasons now writes player_game.parquet alongside the aggregates")
    print("  backup -> export_full.py.bak_pg")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
