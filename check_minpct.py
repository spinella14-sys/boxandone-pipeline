#!/usr/bin/env python3
"""
check_minpct.py — read-only. Can MIN% (share of the minutes available in the
games a player was active for) be computed accurately from what we hold?

Active = he played, or he sat with a coach's-decision DNP. Ruled out = any
DNP that names an injury, illness, rest, personal reason, suspension, not
dressing or not being with the team. Players on the inactive list are not in
the box score at all, so they are never active. Game length is 48 minutes plus
5 per overtime.

Prints:
  1. every DNP reason we hold, by source, with how it is classified
  2. the injury report against the box score (2021-22 on): players listed OUT
     whom the box score still calls active, and the reverse
  3. pool sizes, regular season, at several MIN% lines and game floors, beside
     today's 500-minute pool, plus the players nearest each line

    python3 check_minpct.py
    python3 check_minpct.py --seasons 2024-25,2025-26
"""
import argparse
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import parse_assists as PA  # noqa: E402

OUT_PAT = r"(injur|illness|dnd|dress|nwt|not with team|inactive|suspen|personal|rest|g league|g-league|two-way|health|protocol|conditioning|surgery|recover)"

CLASSIFY = """
CREATE OR REPLACE TEMP VIEW box AS
SELECT b.player_id, b.game_id, b.team_abbr, b.source, b.played, b.dnp_reason,
       COALESCE(b.seconds_played, 0) AS secs,
       g.season, g.season_type, g.game_date, 48 + 5 * COALESCE(g.overtimes, 0) AS glen,
       CASE WHEN b.played THEN 'played'
            WHEN regexp_matches(lower(COALESCE(b.dnp_reason, '')), '%s') THEN 'out'
            ELSE 'coach' END AS cls
FROM all_box b JOIN all_games g ON g.game_id = b.game_id
WHERE upper(COALESCE(g.league, 'NBA')) = 'NBA'
""" % OUT_PAT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023-24,2024-25,2025-26")
    a = ap.parse_args()
    seasons = a.seasons.split(",")
    con = PA.connect()
    con.execute(CLASSIFY)

    print("\n1. DNP reasons we hold (rows), and how each is read")
    total = con.execute("SELECT COUNT(*), MIN(season), MAX(season) FROM box").fetchone()
    print("   %d player-game rows, %s to %s" % total)
    for src, reason, cls, n, lo, hi in con.execute("""
        SELECT source, COALESCE(dnp_reason, '(none)'), cls, COUNT(*), MIN(season), MAX(season)
        FROM box WHERE NOT played GROUP BY ALL ORDER BY 1, 4 DESC""").fetchall():
        print("   %-10s %-44s %-6s %8d  %s to %s" % (src, reason[:44], cls, n, lo, hi))

    print("\n2. Injury report against the box score (players listed OUT for that game)")
    try:
        con.execute("""CREATE OR REPLACE TEMP VIEW inj AS
            SELECT game_date, player_id, upper(status) AS status FROM hot.injury_rows WHERE player_id IS NOT NULL""")
        con.execute("CREATE OR REPLACE TEMP VIEW named AS SELECT * FROM box")
        for row in con.execute("""
            SELECT b.season, COUNT(*) AS listed_out,
                   SUM(CASE WHEN b.cls = 'out' THEN 1 ELSE 0 END) AS box_out,
                   SUM(CASE WHEN b.cls = 'coach' THEN 1 ELSE 0 END) AS box_coach_dnp,
                   SUM(CASE WHEN b.cls = 'played' THEN 1 ELSE 0 END) AS played_anyway
            FROM named b JOIN inj i ON i.game_date = b.game_date AND i.player_id = b.player_id AND i.status = 'OUT'
            GROUP BY 1 ORDER BY 1""").fetchall():
            print("   %s  listed out and in the box score: %5d · box says out %5d · coach's DNP %4d · played %3d" % row)
        print("   (players listed out and absent from the box score entirely are the inactive list: correct by construction)")
        print("   a few coach's-decision DNPs the report had as OUT:")
        for row in con.execute("""
            SELECT b.game_date, b.team_abbr, b.player_id, b.dnp_reason FROM named b
            JOIN inj i ON i.game_date = b.game_date AND i.player_id = b.player_id AND i.status = 'OUT'
            WHERE b.cls = 'coach' ORDER BY random() LIMIT 8""").fetchall():
            print("     %s %s %-24s %s" % row)
    except Exception as e:
        print("   skipped (%s)" % str(e).split("\n")[0])

    print("\n3. Regular-season pools")
    for s in seasons:
        con.execute("""CREATE OR REPLACE TEMP TABLE q AS
            SELECT b.player_id, ANY_VALUE(p.full_name) AS name,
                   SUM(secs) / 60.0 AS mp,
                   COUNT(*) FILTER (WHERE cls <> 'out') AS active,
                   COUNT(*) FILTER (WHERE cls = 'played') AS gp,
                   100 * SUM(secs) / 60.0 / NULLIF(SUM(glen) FILTER (WHERE cls <> 'out'), 0) AS minpct
            FROM box b JOIN hot.players p USING (player_id)
            WHERE season = ? AND season_type = 'regular' GROUP BY 1""", [s])
        tg = con.execute("""SELECT MAX(n) FROM (SELECT team_abbr, COUNT(DISTINCT game_id) n FROM box
                            WHERE season = ? AND season_type = 'regular' GROUP BY 1)""", [s]).fetchone()[0] or 0
        n500 = con.execute("SELECT COUNT(*) FROM q WHERE mp >= 500").fetchone()[0]
        nall = con.execute("SELECT COUNT(*) FROM q WHERE gp > 0").fetchone()[0]
        print("\n   %s (%d team games so far): %d played; 500+ minutes: %d" % (s, tg, nall, n500))
        print("     MIN%   any games   active 20+   active 25% of team games   the rule: 25% of games OR 300 min")
        for line in (10, 15, 20, 25):
            r = con.execute("""SELECT COUNT(*) FILTER (WHERE minpct >= ?),
                                      COUNT(*) FILTER (WHERE minpct >= ? AND active >= 20),
                                      COUNT(*) FILTER (WHERE minpct >= ? AND active >= ?),
                                      COUNT(*) FILTER (WHERE minpct >= ? AND (active >= ? OR mp >= 300))
                               FROM q""", [line, line, line, 0.25 * tg, line, 0.25 * tg]).fetchone()
            print("     %3d%%   %9d   %10d   %24d   %16d" % ((line,) + r))
        print("     who sits near the lines (active 20+):")
        for line in (10, 20):
            near = con.execute("""SELECT name, minpct, active, mp FROM q
                                  WHERE active >= 20 AND minpct BETWEEN ? AND ? ORDER BY minpct LIMIT 6""",
                               [line - 1, line + 1]).fetchall()
            print("       ~%d%%: %s" % (line, "; ".join("%s %.1f%% (%d active, %d min)" % r for r in near)))
        tops = con.execute("SELECT name, minpct, active FROM q WHERE active >= 20 ORDER BY minpct DESC LIMIT 3").fetchall()
        print("       highest: %s" % "; ".join("%s %.1f%% (%d)" % r for r in tops))


if __name__ == "__main__":
    main()
