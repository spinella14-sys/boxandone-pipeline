#!/usr/bin/env python3
"""
patch_playin_fix.py

Replaces the fixed-window play-in rule with a self-calibrating one.

The regular season always ends with a full slate (all 30 teams, 15 games).
Play-in nights carry 2 games. So:

    last_dense   = latest date before the playoffs with >= 5 games
    play-in      = games after last_dense, before the first playoff game

Works for every season. Pre-2020 there is no gap, so play-in resolves to 0
without needing a special case.

Also flags the NBA Cup championship game. It is played but does not count
toward regular season records, so it belongs in 'tournament', not 'regular'.
Detected as a lone December game on a date with no other games, in seasons
from 2023-24 onward.

Run:  python3 patch_playin_fix.py
No network requests. Safe to rerun.
"""

import os
import sys

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
TARGET = os.path.join(HOME, "patch_season_type.py")

CUP_FIRST_SEASON = 2024        # 2023-24 was the inaugural In-Season Tournament


def relabel():
    import duckdb
    con = duckdb.connect(DB_PATH)

    seasons = [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM ingest_log ORDER BY season DESC").fetchall()]

    print(f"  {'season':10} {'regular':>9} {'playin':>7} {'playoff':>8} {'cup':>4}")

    for label in seasons:
        end_year = int(label.split("-")[0]) + 1

        first_po = con.execute(
            "SELECT MIN(game_date) FROM ingest_log "
            "WHERE season=? AND season_type='playoff'", [label]).fetchone()[0]

        # reset everything that isn't playoff back to regular
        con.execute("UPDATE ingest_log SET season_type='regular' "
                    "WHERE season=? AND season_type<>'playoff'", [label])

        playin = 0
        if first_po:
            last_dense = con.execute("""
                SELECT MAX(game_date) FROM (
                    SELECT game_date FROM ingest_log
                    WHERE season=? AND game_date < ?
                    GROUP BY game_date HAVING COUNT(*) >= 5
                )
            """, [label, first_po]).fetchone()[0]

            if last_dense:
                con.execute("""
                    UPDATE ingest_log SET season_type='playin'
                    WHERE season=? AND season_type='regular'
                      AND game_date > ? AND game_date < ?
                """, [label, last_dense, first_po])
                playin = con.execute(
                    "SELECT COUNT(*) FROM ingest_log "
                    "WHERE season=? AND season_type='playin'", [label]).fetchone()[0]

        # NBA Cup final: a solitary December game
        cup = 0
        if end_year >= CUP_FIRST_SEASON:
            con.execute("""
                UPDATE ingest_log SET season_type='tournament'
                WHERE season=? AND season_type='regular'
                  AND game_date IN (
                      SELECT game_date FROM ingest_log
                      WHERE season=? AND MONTH(game_date)=12
                      GROUP BY game_date HAVING COUNT(*)=1
                  )
            """, [label, label])
            cup = con.execute(
                "SELECT COUNT(*) FROM ingest_log "
                "WHERE season=? AND season_type='tournament'", [label]).fetchone()[0]

        reg = con.execute("SELECT COUNT(*) FROM ingest_log "
                          "WHERE season=? AND season_type='regular'", [label]).fetchone()[0]
        po = con.execute("SELECT COUNT(*) FROM ingest_log "
                         "WHERE season=? AND season_type='playoff'", [label]).fetchone()[0]

        flag = "" if reg == 1230 or not first_po else f"   <- expected 1230"
        print(f"  {label:10} {reg:>9,} {playin:>7} {po:>8} {cup:>4}{flag}")

    con.execute("""
        UPDATE games SET season_type = l.season_type
        FROM ingest_log l WHERE l.game_id = games.game_id
    """)
    con.commit()

    print("\n  boundary dates:")
    for label in seasons:
        rows = con.execute("""
            SELECT season_type, MIN(game_date), MAX(game_date), COUNT(*)
            FROM ingest_log WHERE season=? GROUP BY 1
            ORDER BY MIN(game_date)
        """, [label]).fetchall()
        for r in rows:
            print(f"    {label}  {r[0]:11} {r[1]} -> {r[2]}  ({r[3]:,})")
    con.close()


def patch_source():
    """Make patch_season_type.py use the density rule if it is ever rerun."""
    if not os.path.exists(TARGET):
        print("    patch_season_type.py not found — skipping")
        return
    src = open(TARGET, encoding="utf-8").read()
    if "last_dense" in src:
        print("    already patched")
        return

    old = """                lo = first_po - timedelta(days=PLAYIN_WINDOW_DAYS)
                con.execute(\"\"\"
                    UPDATE ingest_log SET season_type='playin'
                    WHERE season=? AND season_type='regular'
                      AND game_date >= ? AND game_date < ?
                \"\"\", [label, lo, first_po])"""
    new = """                last_dense = con.execute(\"\"\"
                    SELECT MAX(game_date) FROM (
                        SELECT game_date FROM ingest_log
                        WHERE season=? AND game_date < ?
                        GROUP BY game_date HAVING COUNT(*) >= 5)
                \"\"\", [label, first_po]).fetchone()[0]
                con.execute(\"\"\"
                    UPDATE ingest_log SET season_type='playin'
                    WHERE season=? AND season_type='regular'
                      AND game_date > ? AND game_date < ?
                \"\"\", [label, last_dense, first_po])"""

    if old in src:
        open(TARGET + ".bak2", "w", encoding="utf-8").write(src)
        open(TARGET, "w", encoding="utf-8").write(src.replace(old, new))
        print("    play-in rule replaced with density detection")
    else:
        print("    pattern not found — patch_season_type.py left unchanged")


if __name__ == "__main__":
    print("1. relabeling games")
    relabel()
    print("\n2. patching patch_season_type.py")
    patch_source()
    print("\ndone")
