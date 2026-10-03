#!/usr/bin/env python3
"""
patch_pbp_key.py

Two bugs found on real data, one of them silent.

1. PRIMARY KEY (game_id, event_num) used actionNumber, which is NOT unique.
   486 events carried only 453 distinct action numbers, so ON CONFLICT DO
   NOTHING dropped ~5% of every game with no error. actionId IS unique.

   But actionNumber is not junk — it is the EVENT GROUPING key. NBA files
   causally linked events under one action number:

       actionNumber 13  -> Turnover (Beal Bad Pass) + STEAL (Allen)
       actionNumber 80  -> Missed Shot (Beal)       + BLOCK (Allen)

   which is almost certainly how assists attach to made shots too. So it is
   kept as event_group, and the key moves to action_id.

2. personId carries TEAM ids (1610612xxx) on team rebounds, timeouts and
   shot-clock violations — 676 events across 30 teams. Those were being
   stored as if a player with that id existed. They now move to team_id and
   leave nba_person_id NULL.

Re-parse is free; raw JSON is cached.

Run:  python3 patch_pbp_key.py
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/ingest_pbp.py")
DB_PATH = os.path.expanduser("~/boxandone/data/boxandone.duckdb")

TEAM_LO, TEAM_HI = 1610612700, 1610612800


def patch_source():
    src = open(TARGET, encoding="utf-8").read()
    if "event_group" in src:
        print("    already patched")
        return

    # --- table definition -------------------------------------------------
    src = src.replace(
        "            game_id          VARCHAR NOT NULL,\n"
        "            event_num        INTEGER NOT NULL,",
        "            game_id          VARCHAR NOT NULL,\n"
        "            action_id        BIGINT  NOT NULL,   -- unique within game\n"
        "            event_group      INTEGER,            -- NBA actionNumber;\n"
        "                                                 -- links cause+effect\n"
        "                                                 -- (steal<-turnover,\n"
        "                                                 --  block<-miss,\n"
        "                                                 --  assist<-made shot)")
    src = src.replace("            PRIMARY KEY (game_id, event_num)",
                      "            PRIMARY KEY (game_id, action_id)")
    # the old standalone action_id column added by the fields patch
    src = src.replace("            location         VARCHAR,      -- 'h' / 'v'\n"
                      "            action_id        BIGINT,",
                      "            location         VARCHAR,      -- 'h' / 'v'")

    src = src.replace(
        "CREATE INDEX IF NOT EXISTS idx_pbp_game ON play_by_play(game_id)",
        "CREATE INDEX IF NOT EXISTS idx_pbp_game ON play_by_play(game_id)\")\n"
        "    con.execute(\"CREATE INDEX IF NOT EXISTS idx_pbp_group "
        "ON play_by_play(game_id, event_group)")

    # --- insert column list ----------------------------------------------
    src = src.replace(
        "              (game_id,event_num,period,clock_raw,seconds_left,elapsed_seconds,",
        "              (game_id,action_id,event_group,period,clock_raw,seconds_left,elapsed_seconds,")
    src = src.replace(
        "               action_type,sub_type,description,shot_result,shot_value,location,\n"
        "               action_id,shot_distance,",
        "               action_type,sub_type,description,shot_result,shot_value,location,\n"
        "               shot_distance,")
    src = src.replace("            ON CONFLICT (game_id,event_num) DO NOTHING",
                      "            ON CONFLICT (game_id,action_id) DO NOTHING")

    # --- row construction -------------------------------------------------
    src = src.replace(
        '''            pid_nba = _num(a.get("personId"), zero_is_null=True)
            team_id = _num(a.get("teamId"), zero_is_null=True)''',
        '''            pid_nba = _num(a.get("personId"), zero_is_null=True)
            team_id = _num(a.get("teamId"), zero_is_null=True)
            # personId carries TEAM ids on team rebounds, timeouts, violations
            if pid_nba is not None and 1610612700 <= pid_nba <= 1610612800:
                if team_id is None:
                    team_id = pid_nba
                pid_nba = None''')

    src = src.replace(
        '''                gid, _num(a.get("actionNumber")), per, a.get("clock") or None, left,''',
        '''                gid, _num(a.get("actionId")), _num(a.get("actionNumber")),
                per, a.get("clock") or None, left,''')
    src = src.replace('''                _num(a.get("actionId")),
                _dec(a.get("shotDistance")) if is_fg else None,''',
                      '''                _dec(a.get("shotDistance")) if is_fg else None,''')

    src = src.replace("ORDER BY event_num", "ORDER BY action_id")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("    patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_key", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("    PK -> (game_id, action_id)")
    print("    actionNumber kept as event_group (links cause+effect)")
    print("    team ids in personId now routed to team_id")
    print("    backup -> ingest_pbp.py.bak_key")


def rebuild_table():
    import duckdb
    con = duckdb.connect(DB_PATH)
    con.execute("DROP VIEW IF EXISTS pbp_with_score")
    con.execute("DROP TABLE IF EXISTS play_by_play")
    con.execute("""
        CREATE TABLE play_by_play (
            game_id          VARCHAR NOT NULL,
            action_id        BIGINT  NOT NULL,
            event_group      INTEGER,
            period           SMALLINT,
            clock_raw        VARCHAR,
            seconds_left     DOUBLE,
            elapsed_seconds  DOUBLE,
            nba_person_id    BIGINT,
            player_id        VARCHAR,
            player_name      VARCHAR,
            team_id          BIGINT,
            team_abbr        VARCHAR,
            action_type      VARCHAR,
            sub_type         VARCHAR,
            description      VARCHAR,
            shot_result      VARCHAR,
            shot_value       SMALLINT,
            location         VARCHAR,
            shot_distance    DOUBLE,
            x_legacy         INTEGER,
            y_legacy         INTEGER,
            is_field_goal    BOOLEAN,
            points_total     INTEGER,
            score_home       INTEGER,
            score_away       INTEGER,
            source           VARCHAR NOT NULL DEFAULT 'nba',
            provenance       VARCHAR NOT NULL DEFAULT 'scraped',
            PRIMARY KEY (game_id, action_id)
        )
    """)
    for s in ["CREATE INDEX IF NOT EXISTS idx_pbp_game ON play_by_play(game_id)",
              "CREATE INDEX IF NOT EXISTS idx_pbp_group ON play_by_play(game_id, event_group)",
              "CREATE INDEX IF NOT EXISTS idx_pbp_player ON play_by_play(player_id)",
              "CREATE INDEX IF NOT EXISTS idx_pbp_nbaid ON play_by_play(nba_person_id)"]:
        con.execute(s)

    con.execute("""
        CREATE OR REPLACE VIEW pbp_with_score AS
        SELECT *,
            LAST_VALUE(score_home IGNORE NULLS) OVER w AS run_home,
            LAST_VALUE(score_away IGNORE NULLS) OVER w AS run_away
        FROM play_by_play
        WINDOW w AS (PARTITION BY game_id ORDER BY action_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    """)

    # events sharing an event_group: cause and effect on one line
    con.execute("""
        CREATE OR REPLACE VIEW pbp_linked AS
        SELECT a.game_id, a.event_group, a.action_id AS primary_action,
               a.action_type AS primary_type, a.player_id AS primary_player,
               a.player_name AS primary_name,
               b.action_id AS linked_action, b.description AS linked_desc,
               b.player_id AS linked_player, b.player_name AS linked_name
        FROM play_by_play a
        JOIN play_by_play b
          ON b.game_id = a.game_id AND b.event_group = a.event_group
         AND b.action_id > a.action_id
        WHERE a.action_type IS NOT NULL AND a.action_type <> ''
    """)
    con.execute("UPDATE pbp_log SET parsed=FALSE WHERE parsed")
    con.commit()
    con.close()
    print("    play_by_play rebuilt, views recreated, re-parse queued")


if __name__ == "__main__":
    if not os.path.exists(TARGET):
        sys.exit("%s not found" % TARGET)
    print("1. patching ingest_pbp.py")
    patch_source()
    print("\n2. rebuilding play_by_play")
    rebuild_table()
    print("\ndone. next: python3 ingest_pbp.py parse --season 2025-26")
