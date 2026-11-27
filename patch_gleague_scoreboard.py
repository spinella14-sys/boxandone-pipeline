#!/usr/bin/env python3
"""
patch_gleague_scoreboard.py — run once.

The G League scoreboard needs three things the schedule already carries but
we didn't keep: the tip-off time (UTC), NBA.com's status text ("7:00 pm ET",
"Final", "Final/OT") and the arena. They're stored on gl_games and so reach
each season's games.parquet. A season read from the disk cache before this
patch simply has them blank.

    python3 patch_gleague_scoreboard.py
    python3 -u ingest_gleague.py run --season 2026-27 --sync
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_gleague.py")
src = open(PATH).read()
if "tip_utc" in src:
    print("  ingest_gleague.py already patched")
    sys.exit()

PAIRS = [
    ('''def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for stmt in DDL.split(";"):
        if stmt.strip():
            con.execute(stmt)
    return con''',
     '''def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for stmt in DDL.split(";"):
        if stmt.strip():
            con.execute(stmt)
    for col in ("tip_utc VARCHAR", "status_text VARCHAR", "arena VARCHAR"):
        con.execute("ALTER TABLE gl_games ADD COLUMN IF NOT EXISTS " + col)
    return con'''),
    ('''                "game_id": str(g["gameId"]), "game_date": gd, "status": g.get("gameStatus"),
                "label": label,''',
     '''                "game_id": str(g["gameId"]), "game_date": gd, "status": g.get("gameStatus"),
                "label": label, "tip_utc": g.get("gameDateTimeUTC") or g.get("gameDateUTC"),
                "status_text": (g.get("gameStatusText") or "").strip() or None,
                "arena": ", ".join(x for x in (g.get("arenaName"), g.get("arenaCity")) if x) or None,'''),
    ('''            INSERT INTO gl_games (game_id, season, season_type, label, game_date, home_id, away_id,
                   home_abbr, away_abbr, home_score, away_score, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, now())
            ON CONFLICT (game_id) DO UPDATE SET
                status = excluded.status, game_date = excluded.game_date, label = excluded.label,
                home_score = excluded.home_score, away_score = excluded.away_score,
                updated_at = now()""",
            [g["game_id"], season, stype, g["label"], g["game_date"] or None, g["home"][0], g["away"][0],
             g["home"][1], g["away"][1], g["home"][4], g["away"][4], g["status"]])''',
     '''            INSERT INTO gl_games (game_id, season, season_type, label, game_date, home_id, away_id,
                   home_abbr, away_abbr, home_score, away_score, status, updated_at,
                   tip_utc, status_text, arena)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, now(), ?,?,?)
            ON CONFLICT (game_id) DO UPDATE SET
                status = excluded.status, game_date = excluded.game_date, label = excluded.label,
                home_score = excluded.home_score, away_score = excluded.away_score,
                tip_utc = COALESCE(excluded.tip_utc, gl_games.tip_utc),
                status_text = COALESCE(excluded.status_text, gl_games.status_text),
                arena = COALESCE(excluded.arena, gl_games.arena),
                updated_at = now()""",
            [g["game_id"], season, stype, g["label"], g["game_date"] or None, g["home"][0], g["away"][0],
             g["home"][1], g["away"][1], g["home"][4], g["away"][4], g["status"],
             g.get("tip_utc"), g.get("status_text"), g.get("arena")])'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_gleague.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  ingest_gleague.py patched: tip-off time, status and arena kept for the scoreboard")
