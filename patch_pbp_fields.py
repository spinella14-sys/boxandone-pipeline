#!/usr/bin/env python3
"""
patch_pbp_fields.py

The `fields` dump on real data showed three columns worth keeping that the
first version dropped:

  shotValue   2 or 3 on every shot — saves inferring shot type from coordinates
  location    'h' / 'v' — which side the acting team was on
  actionId    NBA's own event key, distinct from actionNumber

It also showed scoreHome/scoreAway are populated on only ~26% of events (the
scoring plays). The running score at an arbitrary event therefore needs a
forward fill, so this adds a view that does it rather than leaving every future
query to rediscover the problem.

Re-parsing is free — raw JSON is already cached on disk.

Run:  python3 patch_pbp_fields.py
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/ingest_pbp.py")
DB_PATH = os.path.expanduser("~/boxandone/data/boxandone.duckdb")


def patch_source():
    src = open(TARGET, encoding="utf-8").read()
    if "shot_value" in src:
        print("    already patched")
        return False

    src = src.replace(
        "            shot_result      VARCHAR,",
        "            shot_result      VARCHAR,\n"
        "            shot_value       SMALLINT,     -- 2 or 3, straight from NBA\n"
        "            location         VARCHAR,      -- 'h' / 'v'\n"
        "            action_id        BIGINT,")

    src = src.replace(
        "                a.get(\"shotResult\"), a.get(\"shotDistance\"),",
        "                a.get(\"shotResult\"),\n"
        "                (a.get(\"shotValue\") or None),\n"
        "                a.get(\"location\") or None,\n"
        "                a.get(\"actionId\"),\n"
        "                a.get(\"shotDistance\"),")

    src = src.replace(
        "               action_type,sub_type,description,shot_result,shot_distance,",
        "               action_type,sub_type,description,shot_result,shot_value,location,\n"
        "               action_id,shot_distance,")
    src = src.replace(
        "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        "            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_fields", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("    added shot_value, location, action_id")
    print("    backup -> ingest_pbp.py.bak_fields")
    return True


def patch_db():
    import duckdb
    con = duckdb.connect(DB_PATH)
    cols = [r[1] for r in con.execute("PRAGMA table_info('play_by_play')").fetchall()]
    for name, typ in [("shot_value", "SMALLINT"), ("location", "VARCHAR"),
                      ("action_id", "BIGINT")]:
        if name not in cols:
            con.execute("ALTER TABLE play_by_play ADD COLUMN %s %s" % (name, typ))
            print("    added column %s" % name)

    # scoreHome/scoreAway appear only on scoring plays. Forward-fill so the
    # score at any event is queryable without every caller reinventing it.
    con.execute("""
        CREATE OR REPLACE VIEW pbp_with_score AS
        SELECT *,
            LAST_VALUE(score_home IGNORE NULLS) OVER w AS run_home,
            LAST_VALUE(score_away IGNORE NULLS) OVER w AS run_away
        FROM play_by_play
        WINDOW w AS (PARTITION BY game_id ORDER BY event_num
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """)
    print("    created view pbp_with_score (running score, forward-filled)")

    n = con.execute("SELECT COUNT(*) FROM play_by_play").fetchone()[0]
    if n:
        con.execute("DELETE FROM play_by_play")
        con.execute("UPDATE pbp_log SET parsed=FALSE WHERE parsed")
        print("    cleared %d rows and queued re-parse (raw JSON is cached)" % n)
    con.commit()
    con.close()


if __name__ == "__main__":
    if not os.path.exists(TARGET):
        sys.exit("%s not found" % TARGET)
    print("1. patching ingest_pbp.py")
    patch_source()
    print("\n2. updating database")
    patch_db()
    print("\ndone. next: python3 ingest_pbp.py parse --season 2025-26")
