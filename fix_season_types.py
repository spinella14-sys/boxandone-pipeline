#!/usr/bin/env python3
"""
fix_season_types.py — find games filed under the wrong season type and refile them.

Game 5 of the 2026 Finals (June 13, Knicks at Spurs) is held as a regular-season
game, so the bracket stops at 3-1 and the Knicks' and Spurs' regular-season
lines carry a Finals game. Any regular-season game dated after a season's
first playoff game is the same mistake: the regular season is over by then.

    python3 fix_season_types.py check            # list them, change nothing
    python3 fix_season_types.py fix              # refile them, re-export, push

fix runs like the nightly: it takes the lock, pulls the database from R2,
corrects season_type wherever a table carries it (games, ingest_log, and any
other table keyed by game_id), then re-exports each affected season — season
aggregates, BPM, the games index (which the bracket reads), possessions, and
the career files of every player in a refiled game — and pushes the database
back.
"""
import argparse
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

FIND = """
    WITH first_po AS (
        SELECT season, MIN(game_date) AS d FROM games
        WHERE season_type IN ('playoff', 'playin') GROUP BY season)
    SELECT g.game_id, g.season, g.game_date, g.away_abbr, g.home_abbr,
           (SELECT MIN(game_date) FROM games x WHERE x.season = g.season AND x.season_type = 'playoff') AS po
    FROM games g JOIN first_po f USING (season)
    WHERE g.season_type = 'regular' AND g.game_date > f.d
    ORDER BY g.game_date"""


def wrong(con):
    rows = con.execute(FIND).fetchall()
    out = []
    for gid, season, d, a, h, po in rows:
        # after the first playoff game: playoff if the playoffs had begun, else play-in
        out.append((gid, season, d, a, h, "playoff" if po and d >= po else "playin"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "fix"])
    a = ap.parse_args()
    import duckdb
    import nightly2 as N

    if a.cmd == "check":
        con = duckdb.connect(N.DB_PATH, read_only=True)
        rows = wrong(con)
        con.close()
        for gid, s, d, aw, h, t in rows:
            print("  %s  %s  %s at %s  regular -> %s" % (s, d, aw, h, t))
        print("  %d game(s) to refile" % len(rows))
        return

    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  another run holds the lock — try again when it finishes")
    ok = True
    try:
        if not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
        con = duckdb.connect(N.DB_PATH)
        rows = wrong(con)
        if not rows:
            print("  nothing to refile")
            con.close()
            return
        tables = [t for (t,) in con.execute("""
            SELECT table_name FROM duckdb_columns() WHERE column_name = 'season_type'
            INTERSECT SELECT table_name FROM duckdb_columns() WHERE column_name = 'game_id'""").fetchall()]
        for gid, s, d, aw, h, t in rows:
            for tbl in tables:
                con.execute("UPDATE %s SET season_type = ? WHERE game_id = ?" % tbl, [t, gid])
            print("  refiled %s %s at %s (%s) as %s in %s" % (d, aw, h, s, t, ", ".join(tables)))
        players = {}
        for gid, s, *_ in rows:
            for (pid,) in con.execute("SELECT DISTINCT player_id FROM player_game_box WHERE game_id = ?",
                                      [gid]).fetchall():
                players.setdefault(s, set()).add(pid)
        con.commit()
        con.close()

        for season in sorted({r[1] for r in rows}):
            log("re-exporting %s" % season)
            N.export_season(env, log, season)
            ok &= N.run(log, ["compute_bpm.py", "--season", season])
            ok &= N.run(log, ["export_games_index.py", "build", "--season", season])
            ok &= N.run(log, ["parse_possessions_full.py", "build", "--season", season])
            N.export_changed_players(env, log, season, sorted(players.get(season, [])))
        N.rebuild_registry_and_manifest(env, log)
        N.push_db(s3, bucket, log)
        print("  done (%s)" % ("ok" if ok else "with errors"))
    finally:
        N.release_lock(s3, bucket)


if __name__ == "__main__":
    main()
