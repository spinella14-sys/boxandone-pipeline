#!/usr/bin/env python3
"""
migrate_v2_leagues.py — open the schema for non-NBA leagues.

DuckDB has no ALTER TABLE DROP CONSTRAINT, and it refuses to drop a table that
a foreign key points at. So changing a CHECK means rebuilding the table and
everything referencing it, in dependency order. That is what this does.

Three changes:

 1. NEW  `leagues` table. One row per competition, with tier and a competition
    discount factor. This is what makes "Tier 1-3 international" real instead
    of a note in a document, and gives JARED's confidence layer somewhere to
    read a discount from.

 2. `player_identifiers.source` — CHECK list removed. New sources (RealGM
    international, NBL, Proballers, FIBA) no longer need a code change.

 3. `games.league` — CHECK list removed, now references leagues(league_id).
    Every non-North-American competition previously shared one bucket, 'INTL'.

Ingests nothing. Touches no existing row values.

  python3 migrate_v2_leagues.py --dry-run     # show plan, change nothing
  python3 migrate_v2_leagues.py

Requires exclusive DB access — will not run while a fetch is in progress.
Writes a backup copy of the database file first.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")


# league_id, name, country, region, tier, level, discount, pbp
SEED_LEAGUES = [
    ("NBA",      "National Basketball Association", "USA", "North America", 1, "pro", 1.00, True),
    ("GLEAGUE",  "NBA G League",                    "USA", "North America", 3, "pro", 0.75, True),
    ("NCAA",     "NCAA Division I",                 "USA", "North America", None, "college", None, False),
    ("YOUTH",    "Grassroots / Youth",              None,  None,            None, "youth", None, False),
    # ---- Continental competitions. These run CONCURRENTLY with domestic
    # leagues: a Real Madrid player has an ACB season and a EuroLeague season
    # in the same year, against different competition. Game grain keeps them
    # as separate rows, which is correct — do not blend them.
    ("EUROLEAGUE", "EuroLeague",                    None,  "Europe",        None, "pro", None, False),
    ("EUROCUP",    "EuroCup",                       None,  "Europe",        None, "pro", None, False),
    ("BCL",        "Basketball Champions League",   None,  "Europe",        None, "pro", None, False),

    # ---- Domestic leagues
    ("ACB",        "Liga ACB",                      "Spain",     "Europe",  None, "pro", None, False),
    ("LEGA",       "Lega Basket Serie A",           "Italy",     "Europe",  None, "pro", None, False),
    ("BBL_DE",     "Basketball Bundesliga",         "Germany",   "Europe",  None, "pro", None, False),
    ("LNB_FR",     "LNB Pro A / Betclic Elite",     "France",    "Europe",  None, "pro", None, False),
    ("BSL_TR",     "Basketbol Super Ligi",          "Turkey",    "Europe",  None, "pro", None, False),
    ("GREECE",     "Greek Basket League",           "Greece",    "Europe",  None, "pro", None, False),
    ("ADRIATIC",   "ABA League",                    "Serbia",    "Europe",  None, "pro", None, False),
    ("VTB",        "VTB United League",             None,        "Europe",  None, "pro", None, False),
    ("ISRAEL",     "Israeli Basketball Premier League", "Israel", "Europe", None, "pro", None, False),
    ("NBL_AU",     "National Basketball League",    "Australia", "Oceania", None, "pro", None, False),
    ("NZNBL",      "New Zealand NBL",               "New Zealand","Oceania",None, "pro", None, False),
    ("CEBL",       "Canadian Elite Basketball League", "Canada", "North America", None, "pro", None, False),
    ("BSN_PR",     "Baloncesto Superior Nacional",  "Puerto Rico","North America", None, "pro", None, False),
    ("LNBP",       "Liga Nacional de Baloncesto Profesional", "Mexico", "North America", None, "pro", None, False),
    ("CBA_CN",     "Chinese Basketball Association","China",     "Asia",    None, "pro", None, False),
    ("BLEAGUE",    "B.League",                      "Japan",     "Asia",    None, "pro", None, False),
    ("KBL",        "Korean Basketball League",      "South Korea","Asia",   None, "pro", None, False),
    ("LNB_AR",     "Liga Nacional de Basquet",      "Argentina", "South America", None, "pro", None, False),
    ("NBB",        "Novo Basquete Brasil",          "Brazil",    "South America", None, "pro", None, False),
    ("BAL",        "Basketball Africa League",      None,        "Africa",  None, "pro", None, False),

    # ---- National team competition
    ("FIBA",       "FIBA National Team Competition", None,       None,      None, "national_team", None, False),
]


def plan(con):
    print("  current state:")
    for t in ("players", "player_identifiers", "games", "player_game_box"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"    {t:22} {n:>10,} rows")
        except Exception:
            print(f"    {t:22} (missing)")
    has_leagues = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='leagues'"
    ).fetchone()[0]
    print(f"\n  leagues table exists: {bool(has_leagues)}")
    print("\n  will:")
    print("    1. create leagues + seed 28 rows (international tiers left NULL)")
    print("    2. rebuild player_identifiers without the source CHECK")
    print("    3. rebuild games without the league CHECK, FK -> leagues")
    print("       (player_game_box is dropped and restored to allow the games rebuild)")


def migrate(con):
    # ---- 1. leagues -------------------------------------------------------
    con.execute("""
        CREATE TABLE IF NOT EXISTS leagues (
            league_id       VARCHAR PRIMARY KEY,
            name            VARCHAR NOT NULL,
            country         VARCHAR,
            region          VARCHAR,
            tier            INTEGER,
            level           VARCHAR CHECK (level IN ('pro','college','youth','national_team')),
            discount_factor DOUBLE,
            pbp_available   BOOLEAN DEFAULT FALSE,
            first_season    VARCHAR,
            active          BOOLEAN DEFAULT TRUE,
            notes           VARCHAR
        )
    """)
    for row in SEED_LEAGUES:
        con.execute("""
            INSERT INTO leagues (league_id,name,country,region,tier,level,discount_factor,pbp_available)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (league_id) DO NOTHING
        """, list(row))
    n = con.execute("SELECT COUNT(*) FROM leagues").fetchone()[0]
    print(f"    leagues: {n} rows")

    # ---- 2. player_identifiers (drop source CHECK) ------------------------
    con.execute("""
        CREATE TABLE player_identifiers_new (
            source        VARCHAR NOT NULL,
            source_id     VARCHAR NOT NULL,
            player_id     VARCHAR NOT NULL REFERENCES players(player_id),
            source_url    VARCHAR,
            is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
            confidence    DOUBLE  NOT NULL DEFAULT 1.0,
            linked_by     VARCHAR,
            linked_at     TIMESTAMP NOT NULL DEFAULT now(),
            provenance    VARCHAR NOT NULL DEFAULT 'scraped',
            PRIMARY KEY (source, source_id)
        )
    """)
    con.execute("INSERT INTO player_identifiers_new SELECT * FROM player_identifiers")
    moved = con.execute("SELECT COUNT(*) FROM player_identifiers_new").fetchone()[0]
    con.execute("DROP TABLE player_identifiers")
    con.execute("ALTER TABLE player_identifiers_new RENAME TO player_identifiers")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ident_player ON player_identifiers(player_id)")
    print(f"    player_identifiers rebuilt: {moved:,} rows, source CHECK removed")

    # ---- 3. games (drop league CHECK, FK -> leagues) ----------------------
    # player_game_box FKs to games, so it must be moved aside first.
    con.execute("CREATE TABLE pgb_backup AS SELECT * FROM player_game_box")
    kept = con.execute("SELECT COUNT(*) FROM pgb_backup").fetchone()[0]
    con.execute("DROP TABLE player_game_box")

    con.execute("""
        CREATE TABLE games_new (
            game_id         VARCHAR PRIMARY KEY,
            league          VARCHAR NOT NULL REFERENCES leagues(league_id),
            season          VARCHAR NOT NULL,
            game_date       DATE    NOT NULL,
            season_type     VARCHAR NOT NULL DEFAULT 'regular'
                            CHECK (season_type IN
                                  ('preseason','regular','playin','playoff',
                                   'tournament','exhibition','summer_league','national_team')),
            home_team_id    VARCHAR,
            away_team_id    VARCHAR,
            home_abbr       VARCHAR,
            away_abbr       VARCHAR,
            home_score      INTEGER,
            away_score      INTEGER,
            overtimes       INTEGER DEFAULT 0,
            attendance      INTEGER,
            arena           VARCHAR,
            source          VARCHAR NOT NULL,
            source_game_id  VARCHAR,
            source_url      VARCHAR,
            ingested_at     TIMESTAMP NOT NULL DEFAULT now(),
            box_complete    BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    con.execute("INSERT INTO games_new SELECT * FROM games")
    g = con.execute("SELECT COUNT(*) FROM games_new").fetchone()[0]
    con.execute("DROP TABLE games")
    con.execute("ALTER TABLE games_new RENAME TO games")
    con.execute("CREATE INDEX IF NOT EXISTS idx_games_date   ON games(game_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_games_season ON games(league, season)")
    print(f"    games rebuilt: {g:,} rows, league CHECK removed, FK -> leagues")

    con.execute("""
        CREATE TABLE player_game_box (
            player_id VARCHAR NOT NULL REFERENCES players(player_id),
            game_id   VARCHAR NOT NULL REFERENCES games(game_id),
            team_id   VARCHAR, team_abbr VARCHAR NOT NULL, opp_abbr VARCHAR,
            is_home BOOLEAN, started BOOLEAN,
            played BOOLEAN NOT NULL DEFAULT TRUE, dnp_reason VARCHAR,
            seconds_played INTEGER,
            fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
            ftm INTEGER, fta INTEGER, orb INTEGER, drb INTEGER, trb INTEGER,
            ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
            pts INTEGER, plus_minus INTEGER,
            provenance VARCHAR NOT NULL DEFAULT 'scraped'
                       CHECK (provenance IN ('scraped','human','model','derived')),
            source VARCHAR NOT NULL,
            ingested_at TIMESTAMP NOT NULL DEFAULT now(),
            PRIMARY KEY (player_id, game_id)
        )
    """)
    con.execute("INSERT INTO player_game_box SELECT * FROM pgb_backup")
    back = con.execute("SELECT COUNT(*) FROM player_game_box").fetchone()[0]
    con.execute("DROP TABLE pgb_backup")
    con.execute("CREATE INDEX IF NOT EXISTS idx_box_player ON player_game_box(player_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_box_game   ON player_game_box(game_id)")
    if back != kept:
        raise RuntimeError(f"row loss: {kept} before, {back} after")
    print(f"    player_game_box restored: {back:,} rows (verified)")

    # views referencing rebuilt tables
    con.execute("""
        CREATE OR REPLACE VIEW leagues_unset AS
        SELECT league_id, name, country, region, level
        FROM leagues WHERE tier IS NULL OR discount_factor IS NULL
        ORDER BY region, name
    """)
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"{DB_PATH} not found")

    import duckdb
    try:
        con = duckdb.connect(DB_PATH)
    except Exception as e:
        sys.exit(f"\n  cannot open database — is a fetch still running?\n  {str(e)[:120]}")

    if a.dry_run:
        plan(con)
        con.close()
        print("\n  dry run — nothing changed")
        return

    con.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{DB_PATH}.bak_{stamp}"
    shutil.copy2(DB_PATH, backup)
    print(f"  backup -> {os.path.basename(backup)}")

    con = duckdb.connect(DB_PATH)
    try:
        migrate(con)
    except Exception as e:
        con.close()
        shutil.copy2(backup, DB_PATH)
        sys.exit(f"\n  MIGRATION FAILED — database restored from backup\n  {e}")
    con.close()

    print("\n  done. assign tiers with:  SELECT * FROM leagues_unset;")


if __name__ == "__main__":
    main()
