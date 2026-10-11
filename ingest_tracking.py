#!/usr/bin/env python3
"""
ingest_tracking.py — NBA.com player tracking totals, 2013-14 onward.

Camera tracking began league-wide in 2013-14. NBA.com publishes it as season
totals per player (not shot by shot), and these league-wide endpoints return
every player in one request:

  pass      Passing      passes made and received, assists, potential
                         assists, assist points created, secondary and
                         free-throw assists
  poss      Possessions  touches, time of possession, seconds and dribbles
                         per touch, front-court / elbow / post / paint touches
  drib0     shots after 0 dribbles        FGM, FGA, FG%, eFG%, 2s and 3s
  drib1     shots after 1 dribble
  drib2     shots after 2 dribbles
  drib3_6   shots after 3-6 dribbles
  drib7     shots after 7+ dribbles

Seven requests per season per season type. Everything NBA.com returns is
kept, under the measure's prefix and the column's own name lowercased —
pass_potential_ast, poss_avg_drib_per_touch, drib0_fga — so nothing is lost
to a guessed column list. `check` prints what came back.

Writes a long `tracking` table (one row per player, measure and metric) and
v2/seasons/{season}/tracking.parquet, one row per player and season type.

    python3 ingest_tracking.py check --season 2024-25    # fetch + show columns, no writes
    python3 ingest_tracking.py build --season 2024-25
    python3 ingest_tracking.py build --all

The season still being played is always refetched; finished seasons are read
from raw/tracking/ once fetched.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "raw", "tracking")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"
FIRST = 2013          # 2013-14
DELAY = 1.6

HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}

TYPES = {"Regular Season": "regular", "Playoffs": "playoff"}

BASE_PT = {
    "College": "", "Conference": "", "Country": "", "DateFrom": "", "DateTo": "",
    "Division": "", "DraftPick": "", "DraftYear": "", "GameScope": "", "Height": "",
    "LastNGames": "0", "LeagueID": "00", "Location": "", "Month": "0",
    "OpponentTeamID": "0", "Outcome": "", "PORound": "0", "PerMode": "Totals",
    "PlayerExperience": "", "PlayerOrTeam": "Player", "PlayerPosition": "",
    "SeasonSegment": "", "StarterBench": "", "TeamID": "0", "VsConference": "",
    "VsDivision": "", "Weight": "",
}
BASE_SHOT = {
    "CloseDefDistRange": "", "College": "", "Conference": "", "Country": "",
    "DateFrom": "", "DateTo": "", "Division": "", "DraftPick": "", "DraftYear": "",
    "GameScope": "", "GameSegment": "", "GeneralRange": "", "Height": "",
    "LastNGames": "0", "LeagueID": "00", "Location": "", "Month": "0",
    "OpponentTeamID": "0", "Outcome": "", "PORound": "0", "PaceAdjust": "N",
    "PerMode": "Totals", "Period": "0", "PlayerExperience": "", "PlayerPosition": "",
    "PlusMinus": "N", "Rank": "N", "SeasonSegment": "", "ShotClockRange": "",
    "ShotDistRange": "", "StarterBench": "", "TeamID": "0", "TouchTimeRange": "",
    "VsConference": "", "VsDivision": "", "Weight": "",
}

MEASURES = [
    ("pass", "leaguedashptstats", dict(BASE_PT, PtMeasureType="Passing")),
    ("poss", "leaguedashptstats", dict(BASE_PT, PtMeasureType="Possessions")),
    ("drib0", "leaguedashplayerptshot", dict(BASE_SHOT, DribbleRange="0 Dribbles")),
    ("drib1", "leaguedashplayerptshot", dict(BASE_SHOT, DribbleRange="1 Dribble")),
    ("drib2", "leaguedashplayerptshot", dict(BASE_SHOT, DribbleRange="2 Dribbles")),
    ("drib3_6", "leaguedashplayerptshot", dict(BASE_SHOT, DribbleRange="3-6 Dribbles")),
    ("drib7", "leaguedashplayerptshot", dict(BASE_SHOT, DribbleRange="7+ Dribbles")),
]

# identity and label columns, not metrics
SKIP = {"PLAYER_ID", "PLAYER_NAME", "PLAYER_LAST_TEAM_ID", "PLAYER_LAST_TEAM_ABBREVIATION",
        "TEAM_ID", "TEAM_ABBREVIATION", "TEAM_NAME", "AGE", "PLAYER_POSITION",
        "NICKNAME", "PLAYER_FIRST_NAME", "PLAYER_LAST_NAME"}


def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME"):
        env.setdefault(k, os.environ.get(k, ""))
    return env


def label(y):
    return "%d-%s" % (y, str(y + 1)[2:])


def current_season():
    d = datetime.now()
    return label(d.year if d.month >= 10 else d.year - 1)


def fetch(measure, endpoint, params, season, stype, force):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%s_%s.json" % (season, stype.replace(" ", "_"), measure))
    if os.path.exists(path) and not force:
        return json.load(open(path, encoding="utf-8"))
    from curl_cffi import requests as cr
    q = dict(params, Season=season, SeasonType=stype)
    url = "https://stats.nba.com/stats/%s" % endpoint
    for attempt in range(4):
        try:
            r = cr.get(url, params=q, headers=HEADERS, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s %s %s: %s" % (season, stype, measure, str(e)[:70]))
            time.sleep(10 * (attempt + 1))
            continue
        time.sleep(DELAY)
        if r.status_code == 200:
            data = r.json()
            json.dump(data, open(path, "w", encoding="utf-8"))
            return data
        print("      %s %s %s: HTTP %s" % (season, stype, measure, r.status_code))
        time.sleep(10 * (attempt + 1))
    return None


def table_of(data):
    """(headers, rows) from either resultSets shape NBA.com uses."""
    rs = (data or {}).get("resultSets") or (data or {}).get("resultSet")
    if isinstance(rs, dict):
        rs = [rs]
    if not rs:
        return [], []
    return rs[0].get("headers") or [], rs[0].get("rowSet") or []


def long_rows(measure, headers, rows, season, stype):
    if "PLAYER_ID" not in headers:
        return []
    pi = headers.index("PLAYER_ID")
    out = []
    for r in rows:
        for h, v in zip(headers, r):
            if h in SKIP or v is None or isinstance(v, str):
                continue
            out.append((season, TYPES[stype], measure, int(r[pi]), h.lower(), float(v)))
    return out


def seasons_wanted(a):
    cur = current_season()
    if a.all:
        end = int(cur[:4])
        return [label(y) for y in range(FIRST, end + 1)]
    if not a.season:
        sys.exit("  give --season or --all")
    if int(a.season[:4]) < FIRST:
        sys.exit("  tracking starts with 2013-14")
    return [a.season]


def cmd_check(seasons):
    s = seasons[0]
    for stype in TYPES:
        for measure, endpoint, params in MEASURES:
            data = fetch(measure, endpoint, params, s, stype, s == current_season())
            h, rows = table_of(data)
            metrics = [x.lower() for x in h if x not in SKIP]
            print("  %s %-14s %-8s %4d players  %s" % (s, stype, measure, len(rows),
                                                      ", ".join(metrics)[:150]))


def cmd_build(seasons):
    import duckdb
    env = load_env()
    con = duckdb.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS tracking (
        season VARCHAR, season_type VARCHAR, measure VARCHAR,
        nba_person_id BIGINT, metric VARCHAR, value DOUBLE,
        PRIMARY KEY (season, season_type, measure, nba_person_id, metric))""")
    cur = current_season()
    for s in seasons:
        n = 0
        for stype in TYPES:
            for measure, endpoint, params in MEASURES:
                data = fetch(measure, endpoint, params, s, stype, s == cur)
                h, rows = table_of(data)
                lr = long_rows(measure, h, rows, s, stype)
                # replace this slice: a re-fetch can drop a player as well as add one
                con.execute("DELETE FROM tracking WHERE season=? AND season_type=? AND measure=?",
                            [s, TYPES[stype], measure])
                if lr:
                    con.executemany("INSERT INTO tracking VALUES (?,?,?,?,?,?)", lr)
                n += len(lr)
        con.commit()
        print("  %-9s %6d values" % (s, n))
    con.close()
    export(env, seasons)


def export(env, seasons):
    import boto3
    import duckdb
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            have = con.execute("SELECT COUNT(*) FROM hot.tracking WHERE season=?",
                               [s]).fetchone()[0]
            if not have:
                print("  %-9s nothing to export" % s)
                continue
            con.execute("DROP TABLE IF EXISTS w")
            con.execute("""
                CREATE TEMP TABLE w AS
                PIVOT (SELECT i.player_id, t.season_type, t.measure || '_' || t.metric AS k, t.value
                       FROM hot.tracking t
                       JOIN hot.player_identifiers i
                         ON i.source = 'nba' AND i.source_id = CAST(t.nba_person_id AS VARCHAR)
                       WHERE t.season = '%s')
                ON k USING first(value) GROUP BY player_id, season_type""" % s)
            unmapped = con.execute("""
                SELECT COUNT(DISTINCT t.nba_person_id) FROM hot.tracking t
                LEFT JOIN hot.player_identifiers i
                  ON i.source = 'nba' AND i.source_id = CAST(t.nba_person_id AS VARCHAR)
                WHERE t.season = ? AND i.player_id IS NULL""", [s]).fetchone()[0]
            p = os.path.join(tmp, "t.parquet")
            if os.path.exists(p):
                os.remove(p)
            con.execute("COPY w TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % p)
            n = con.execute("SELECT COUNT(*) FROM w").fetchone()[0]
            s3.upload_file(p, env["R2_BUCKET_NAME"], "%s/seasons/%s/tracking.parquet" % (PREFIX, s))
            print("  %-9s tracking.parquet  %d player rows  (%d NBA ids not in the registry)"
                  % (s, n, unmapped))
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    seasons = seasons_wanted(a)
    if a.cmd == "check":
        cmd_check(seasons)
    else:
        cmd_build(seasons)


if __name__ == "__main__":
    main()
