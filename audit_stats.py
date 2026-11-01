#!/usr/bin/env python3
"""
audit_stats.py — read-only. Does the play-by-play agree with the box score,
and is every shot attributed to the right player?

Everything on the site built from play-by-play (shot charts, zones, assisted
rates, tracking-style splits, lineups) is only right if each event is pinned to
the right player. The box score is the independent check: for every
player-season, the field goals in play-by-play should match the box score's.

  1. Identity   a player id that collects more than one NBA person id or name in
                play-by-play (someone else's events filed under him), and an
                NBA person id split across more than one of our ids
  2. Duplicates the same event (game, action) twice in play-by-play, or a made
                shot with two assist rows — either inflates every count
  3. Totals     play-by-play FGA / FGM / 3PA / 3PM against the box score, per
                player-season; the worst disagreements listed, and how many
                per season
  4. One player (--player) the same, season by season, with every name and
                NBA id his events carry

    python3 audit_stats.py                       # the whole archive
    python3 audit_stats.py --player P00000319    # one player in detail
    python3 audit_stats.py --seasons 2023-24:2026-27
"""
import argparse
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)


def season_label(y):
    return "%d-%s" % (y, str(y + 1)[2:])


def seasons_from(spec):
    lo, hi = spec.split(":") if ":" in spec else (spec, spec)
    return [season_label(y) for y in range(int(lo[:4]), int(hi[:4]) + 1)]


def show(rows, head, fmt):
    print(head)
    for r in rows:
        print(fmt % tuple("" if v is None else v for v in r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player")
    ap.add_argument("--seasons")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    import export_full as X
    con = X.connect(X.load_env())
    where = ""
    if a.seasons:
        where = "AND g.season IN (%s)" % ",".join("'%s'" % s for s in seasons_from(a.seasons))

    # the views, narrowed once
    con.execute("""CREATE TEMP TABLE fg AS
        SELECT e.game_id, e.action_id, e.player_id, e.player_name, e.nba_person_id,
               e.shot_result, e.shot_value, g.season
        FROM all_pbp e JOIN all_games g ON g.game_id = e.game_id
        WHERE e.is_field_goal %s""" % where)
    con.execute("""CREATE TEMP TABLE bx AS
        SELECT b.player_id, g.season, SUM(b.fga) fga, SUM(b.fgm) fgm,
               SUM(b.fg3a) fg3a, SUM(b.fg3m) fg3m, COUNT(*) gp
        FROM all_box b JOIN all_games g ON g.game_id = b.game_id
        WHERE b.played %s GROUP BY 1, 2""" % where)
    con.execute("""CREATE TEMP TABLE pb AS
        SELECT player_id, season, COUNT(*) fga,
               COUNT(*) FILTER (WHERE shot_result = 'Made') fgm,
               COUNT(*) FILTER (WHERE shot_value = 3) fg3a,
               COUNT(*) FILTER (WHERE shot_value = 3 AND shot_result = 'Made') fg3m
        FROM fg WHERE player_id IS NOT NULL GROUP BY 1, 2""")

    if a.player:
        pid = a.player
        name = con.execute("SELECT full_name FROM hot.players WHERE player_id = ?", [pid]).fetchone()
        print("\n  %s %s" % (pid, name[0] if name else ""))
        rows = con.execute("""
            SELECT COALESCE(b.season, p.season) s, b.gp, b.fgm, b.fga, p.fgm, p.fga,
                   b.fg3m, b.fg3a, p.fg3m, p.fg3a
            FROM (SELECT * FROM bx WHERE player_id = ?) b
            FULL JOIN (SELECT * FROM pb WHERE player_id = ?) p ON p.season = b.season
            ORDER BY 1""", [pid, pid]).fetchall()
        show(rows, "\n  season     gp   box FG     pbp FG     box 3P   pbp 3P",
             "  %-8s %4s  %4s-%-5s  %4s-%-5s  %3s-%-4s %3s-%-4s")
        rows = con.execute("""
            SELECT season, player_name, nba_person_id, COUNT(*) n FROM fg WHERE player_id = ?
            GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC""", [pid]).fetchall()
        show(rows, "\n  names and NBA ids on his play-by-play field goals",
             "    %-8s %-26s %-10s %5s")
        dup = con.execute("""SELECT COUNT(*) FROM (SELECT game_id, action_id FROM fg
                             WHERE player_id = ? GROUP BY 1, 2 HAVING COUNT(*) > 1)""", [pid]).fetchone()[0]
        print("\n  duplicated events among his field goals: %d" % dup)
        con.close()
        return

    # 1. identity
    print("\n  1. IDENTITY")
    rows = con.execute("""
        SELECT player_id, COUNT(DISTINCT nba_person_id) n_ids, COUNT(DISTINCT player_name) n_names,
               string_agg(DISTINCT player_name, ' / ') who, COUNT(*) n
        FROM fg WHERE player_id IS NOT NULL
        GROUP BY 1 HAVING COUNT(DISTINCT nba_person_id) > 1
        ORDER BY n DESC LIMIT ?""", [a.top]).fetchall()
    n_multi = con.execute("""SELECT COUNT(*) FROM (SELECT player_id FROM fg WHERE player_id IS NOT NULL
                             GROUP BY 1 HAVING COUNT(DISTINCT nba_person_id) > 1)""").fetchone()[0]
    print("  our ids carrying more than one NBA person id: %d" % n_multi)
    show(rows, "    player_id   nba ids  names  field goals  names seen",
         "    %-11s %7s  %5s  %-60s %6s")
    n_split = con.execute("""SELECT COUNT(*) FROM (SELECT nba_person_id FROM fg
                             WHERE player_id IS NOT NULL AND nba_person_id IS NOT NULL
                             GROUP BY 1 HAVING COUNT(DISTINCT player_id) > 1)""").fetchone()[0]
    unb = con.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE player_id IS NULL) FROM fg").fetchone()
    print("  NBA person ids split across several of our ids: %d" % n_split)
    print("  field goals with no player id: %d of %d (%.2f%%)" % (unb[1], unb[0], 100.0 * unb[1] / max(1, unb[0])))

    # 2. duplicates
    print("\n  2. DUPLICATES")
    d1 = con.execute("""SELECT COUNT(*), COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM fg
                        GROUP BY game_id, action_id HAVING COUNT(*) > 1)""").fetchone()
    print("  play-by-play field goals stored more than once: %d events (%d extra rows)" % d1)
    if d1[0]:
        rows = con.execute("""SELECT season, COUNT(*) FROM (SELECT season, game_id, action_id FROM fg
                              GROUP BY 1, 2, 3 HAVING COUNT(*) > 1) GROUP BY 1 ORDER BY 1""").fetchall()
        show(rows, "    by season:", "      %-8s %d")
    d2 = con.execute("""SELECT COUNT(*), COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM all_ast
                        GROUP BY game_id, action_id HAVING COUNT(*) > 1)""").fetchone()
    print("  made shots with more than one assist row: %d (%d extra rows)" % d2)

    # 3. totals
    print("\n  3. PLAY-BY-PLAY AGAINST THE BOX SCORE (player-seasons)")
    con.execute("""CREATE TEMP TABLE cmp AS
        SELECT b.player_id, b.season, b.gp, b.fga bfga, COALESCE(p.fga, 0) pfga,
               b.fgm bfgm, COALESCE(p.fgm, 0) pfgm, b.fg3a bfg3a, COALESCE(p.fg3a, 0) pfg3a
        FROM bx b LEFT JOIN pb p USING (player_id, season)""")
    rows = con.execute("""
        SELECT season, COUNT(*) players,
               COUNT(*) FILTER (WHERE abs(pfga - bfga) > greatest(5, 0.1 * bfga)) fga_off,
               COUNT(*) FILTER (WHERE abs(pfg3a - bfg3a) > greatest(3, 0.1 * bfg3a)) fg3a_off,
               COUNT(*) FILTER (WHERE pfga > 1.25 * bfga + 10) inflated,
               COUNT(*) FILTER (WHERE bfga > 20 AND pfga < 0.75 * bfga) missing,
               round(SUM(pfga) / NULLIF(SUM(bfga), 0), 3) ratio
        FROM cmp GROUP BY 1 ORDER BY 1""").fetchall()
    show(rows, "  season   players  FGA off  3PA off  inflated  missing  pbp/box FGA",
         "  %-8s %7s  %7s  %7s  %8s  %7s  %s")
    rows = con.execute("""
        SELECT c.season, c.player_id, p.full_name, c.gp, c.bfga, c.pfga, c.bfg3a, c.pfg3a
        FROM cmp c LEFT JOIN hot.players p USING (player_id)
        ORDER BY abs(c.pfga - c.bfga) DESC LIMIT ?""", [a.top]).fetchall()
    show(rows, "\n  worst disagreements\n  season   player_id    name                       gp  box FGA pbp FGA  box 3PA pbp 3PA",
         "  %-8s %-11s  %-24s %4s  %7s %7s  %7s %7s")
    con.close()


if __name__ == "__main__":
    main()
