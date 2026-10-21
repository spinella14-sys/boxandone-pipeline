#!/usr/bin/env python3
"""
repair_bubble.py — add the 2020 Finals Games 2-6, which were never ingested.

Basketball Reference splits the bubble season's schedule into two October
pages (2019 and 2020). We only ever asked for "october", so October 2020 —
Finals Games 2 to 6 — was never seen. 2019-20 is in the cold archive, so the
fix runs in this order:

  1. the five games go into the working database the normal way: box scores
     from Basketball Reference, play-by-play from NBA's CDN
  2. their rows are added to the 2019-20 archive (R2 and the copy on this Mac)
     and removed from the working database again
  3. everything those games feed is exported again: 2019-20 season files and
     BPM, assists, each Laker's and Heat player's career game log, shots and
     assists, the games index, the five game pages and their lineups,
     registry and manifest
  4. ingest_games.py learns about the second October, so a rebuild gets it

Holds the database lock throughout. Safe to rerun: every step skips what is
already done.

    python3 repair_bubble.py
"""
import glob
import json
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

SEASON = "2019-20"
NBA_IDS = ["0041900402", "0041900403", "0041900404", "0041900405", "0041900406"]
CACHE = os.path.join(HOME, "data", "archive", SEASON)
TABLES = [  # (table, how its rows tie to a game)
    ("games", "game_id"), ("ingest_log", "game_id"), ("pbp_log", "game_id"),
    ("game_identifiers", "game_id"), ("player_game_box", "game_id"), ("play_by_play", "game_id"),
]
BBREF = {"BKN": "BRK", "PHX": "PHO", "CHA": "CHO"}


def patch_ingest_games(log):
    path = os.path.join(HOME, "ingest_games.py")
    src = open(path).read()
    if "EXTRA_MONTHS" in src:
        return
    old = '''        for month in MONTHS:
'''
    new = '''        for month in MONTHS + EXTRA_MONTHS.get(end_year, []):
'''
    anchor = '''MONTHS = ["october", "november", "december", "january", "february",
          "march", "april", "may", "june", "july", "august", "september"]
'''
    if src.count(old) != 1 or src.count(anchor) != 1:
        log("ingest_games.py not as expected — month fix NOT applied (the repair still runs)")
        return
    src = src.replace(anchor, anchor + '''# the bubble season had two Octobers; "october" is 2019's page
EXTRA_MONTHS = {2020: ["october-2020"]}
''').replace(old, new)
    open(path, "w").write(src)
    log("ingest_games.py now also reads the October 2020 schedule page")


def games_from_box(log):
    import cdn_feed as C
    out = []
    for nba in NBA_IDS:
        g = (C.load(nba, "box", fetch=True) or {}).get("game") or {}
        code = g.get("gameCode") or ""
        home = (g.get("homeTeam") or {}).get("teamTricode")
        if "/" not in code or not home:
            raise RuntimeError("no box score on NBA's CDN for %s" % nba)
        ymd = code[:8]
        slug = "%s0%s" % (ymd, BBREF.get(home, home))
        out.append({"nba": nba, "ours": "NBA_" + slug, "slug": slug,
                    "date": "%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])})
        log("  %s  %s  %s" % (nba, out[-1]["date"], code))
    return out


def ids_sql(games):
    return ",".join("'%s'" % g["ours"] for g in games)


def main():
    import duckdb
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  another run holds the database — try again when it finishes")
    try:
        if not N.pull_db(s3, bucket, log):
            return
        patch_ingest_games(log)
        log("the five games, from NBA's box scores:")
        games = games_from_box(log)

        # ---- 1. into the working database the normal way
        con = duckdb.connect(N.DB_PATH)
        pending = con.execute("SELECT COUNT(*) FROM ingest_log WHERE NOT fetched").fetchone()[0]
        if pending:
            log("note: %d other games were already waiting to be fetched; they come along" % pending)
        for g in games:
            con.execute("""INSERT INTO ingest_log (game_id, league, season, game_date, source_slug, season_type)
                           VALUES (?, 'NBA', ?, ?, ?, 'playoff') ON CONFLICT DO NOTHING""",
                        [g["ours"], SEASON, g["date"], g["slug"]])
            con.execute("""INSERT INTO game_identifiers
                             (source, source_game_id, game_id, season, season_type, game_date, linked_by)
                           VALUES ('nba', ?, ?, ?, 'Playoffs', ?, 'repair_bubble')
                           ON CONFLICT DO NOTHING""", [g["nba"], g["ours"], SEASON, g["date"]])
        con.commit()
        con.close()
        ok = N.run(log, ["ingest_games.py", "fetch"])
        ok &= N.run(log, ["ingest_games.py", "parse"])
        ok &= N.run(log, ["ingest_pbp.py", "fetch", "--season", SEASON, "--source", "cdn"])
        ok &= N.run(log, ["ingest_pbp.py", "parse", "--season", SEASON, "--source", "cdn"])

        con = duckdb.connect(N.DB_PATH)
        got = con.execute("""SELECT g.game_id, g.home_abbr, g.away_abbr, g.home_score, g.away_score,
                                    (SELECT COUNT(*) FROM player_game_box b WHERE b.game_id=g.game_id),
                                    (SELECT COUNT(*) FROM play_by_play e WHERE e.game_id=g.game_id)
                             FROM games g WHERE g.game_id IN (%s) ORDER BY 1""" % ids_sql(games)).fetchall()
        for r in got:
            log("  %s  %s %s - %s %s   %d box rows, %d events" % (r[0], r[1], r[3], r[4], r[2], r[5], r[6]))
        if len(got) != len(games) or any(r[5] == 0 for r in got):
            con.close()
            raise RuntimeError("not all five games landed — stopping before the archive is touched")
        if any(r[6] == 0 for r in got):
            log("WARNING: a game has no play-by-play; its box score still goes in")
        players = [r[0] for r in con.execute(
            "SELECT DISTINCT player_id FROM player_game_box WHERE game_id IN (%s) AND played"
            % ids_sql(games)).fetchall()]

        # ---- 2. into the 2019-20 archive, then out of the working database
        log("adding them to the %s archive" % SEASON)
        os.makedirs(CACHE, exist_ok=True)
        state_path = os.path.join(HOME, "data", "archive_state.json")
        state = json.load(open(state_path)) if os.path.exists(state_path) else {}
        with tempfile.TemporaryDirectory() as tmp:
            for t, col in TABLES:
                n_new = con.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)"
                                    % (t, col, ids_sql(games))).fetchone()[0]
                if not n_new:
                    continue
                key = "v2/archive/%s/%s.parquet" % (SEASON, t)
                old = os.path.join(tmp, "old_%s.parquet" % t)
                try:
                    s3.download_file(bucket, key, old)
                    # keep the untouched copy, in case anything here is wrong
                    bkey = "v2/archive_backup/%s/%s.parquet" % (SEASON, t)
                    try:
                        s3.head_object(Bucket=bucket, Key=bkey)
                    except Exception:
                        s3.upload_file(old, bucket, bkey)
                    base = "SELECT * FROM read_parquet('%s') WHERE %s NOT IN (%s) UNION ALL BY NAME " % (
                        old, col, ids_sql(games))
                except Exception:
                    base = ""
                merged = os.path.join(tmp, "%s.parquet" % t)
                con.execute("COPY (%s SELECT * FROM %s WHERE %s IN (%s)) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                            % (base, t, col, ids_sql(games), merged))
                total = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % merged).fetchone()[0]
                s3.upload_file(merged, bucket, key)
                local = os.path.join(CACHE, "%s.parquet" % t)
                if os.path.exists(local) or t in ("games", "player_game_box", "play_by_play"):
                    duckdb.connect().execute("COPY (SELECT * FROM read_parquet('%s')) TO '%s' "
                                             "(FORMAT PARQUET, COMPRESSION ZSTD)" % (merged, local))
                tables = state.get("archived", {}).get(SEASON, {}).get("tables", {})
                if t in tables:
                    tables[t]["rows"] = total
                log("  %-18s +%d rows -> %d" % (t, n_new, total))
        if state:
            json.dump(state, open(state_path, "w"), indent=2)
        for t, col in reversed(TABLES):          # children first, one commit each:
            con.execute("DELETE FROM %s WHERE %s IN (%s)" % (t, col, ids_sql(games)))
            con.commit()                         # DuckDB checks keys per transaction
        con.close()

        # ---- 3. everything the games feed
        ok &= N.run(log, ["export_full.py", "seasons", "--season", SEASON])      # + BPM
        ok &= N.run(log, ["parse_assists.py", "build", "--season", SEASON])
        import export_full as EF
        econ = EF.connect(env)
        with tempfile.TemporaryDirectory() as tmp:
            for pid in players:
                for sql, name in ((EF.GAMELOG_SQL, "gamelog"), (EF.SHOTS_SQL, "shots"),
                                  (EF.ASSISTS_SQL, "assists")):
                    EF.put(econ, s3, bucket, sql % pid, "v2/players/%s/%s.parquet" % (pid, name), tmp)
        econ.close()
        log("career files rewritten for %d players" % len(players))
        ok &= N.run(log, ["export_games_index.py", "build", "--season", SEASON])
        ok &= N.run(log, ["export_game_files.py", "build", "--games", ",".join(g["ours"] for g in games)])
        ok &= N.run(log, ["export_game_lineups.py", "build", "--seasons", SEASON])
        N.rebuild_registry_and_manifest(env, log)
        N.push_db(s3, bucket, log)
        log("done (%s)" % ("ok" if ok else "with errors — see above"))
    finally:
        N.release_lock(s3, bucket)


if __name__ == "__main__":
    main()
