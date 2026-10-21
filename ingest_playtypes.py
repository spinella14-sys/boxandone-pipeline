#!/usr/bin/env python3
"""
ingest_playtypes.py — play types, defensive tracking and hustle, per player.

Mac only: everything here is on stats.nba.com, which GitHub cannot reach.

  play types   (2015-16 on)  synergyplaytypes, one request per play type
               offense: isolation, P&R ball handler, P&R roll man, post-up,
                        spot-up, handoff, cut, off screen, transition,
                        putbacks, misc
               defense: isolation, P&R ball handler, P&R roll man, post-up,
                        spot-up, handoff, off screen
               kept per play type: possessions, points, FGM, FGA, and the
               counts behind turnover, shooting-foul, and-one and score rates
  defense      (2013-14 on)  leaguedashptdefend — opponents' shooting when he
               is the closest defender: overall, threes, twos, inside 6 ft,
               inside 10 ft, beyond 15 ft
  hustle       (2015-16 on)  leaguehustlestatsplayer — contests, deflections,
               charges, screen assists, loose balls, box-outs

Every numeric column NBA.com returns is kept under its measure's prefix, so
nothing is lost to a guessed column list. Raw responses are cached under
raw/playtypes/; the season under way is always refetched.

Writes v2/seasons/{season}/playtypes.parquet and defense.parquet: one row per
player and season type, keyed by our player_id (and NBA's id).

    python3 ingest_playtypes.py check --season 2024-25   # fetch one season, show columns
    python3 ingest_playtypes.py build --all              # 2013-14 to now
    python3 ingest_playtypes.py build --season 2026-27   # the weekly job
"""
import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import ingest_tracking as T  # noqa: E402  (headers, fetch helpers, env)

CACHE = os.path.join(HOME, "raw", "playtypes")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
TYPES = {"Regular Season": "regular", "Playoffs": "playoff"}

OFF = ["Isolation", "PRBallHandler", "PRRollman", "Postup", "Spotup", "Handoff",
       "Cut", "OffScreen", "Transition", "OffRebound", "Misc"]
DEF = ["Isolation", "PRBallHandler", "PRRollman", "Postup", "Spotup", "Handoff", "OffScreen"]
SHORT = {"Isolation": "iso", "PRBallHandler": "prbh", "PRRollman": "prrm", "Postup": "post",
         "Spotup": "spot", "Handoff": "hand", "Cut": "cut", "OffScreen": "offscr",
         "Transition": "trans", "OffRebound": "putback", "Misc": "misc"}
DEFEND = [("all", "Overall"), ("3pt", "3 Pointers"), ("2pt", "2 Pointers"),
          ("lt6", "Less Than 6Ft"), ("lt10", "Less Than 10Ft"), ("gt15", "Greater Than 15Ft")]
SKIP = T.SKIP | {"CLOSE_DEF_PERSON_ID", "SEASON_ID", "PLAYER_LAST_TEAM_ID",
                 "PLAYER_LAST_TEAM_ABBREVIATION", "PLAY_TYPE", "TYPE_GROUPING", "GP", "G",
                 "PERCENTILE", "TEAM_NAME"}


def fetch(key, endpoint, params, season, stype, force):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%s_%s.json" % (season, stype.replace(" ", "_"), key))
    if os.path.exists(path) and not force:
        return json.load(open(path, encoding="utf-8"))
    from curl_cffi import requests as cr
    url = "https://stats.nba.com/stats/%s" % endpoint
    for attempt in range(4):
        try:
            r = cr.get(url, params=params, headers=T.HEADERS, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s %s %s: %s" % (season, stype, key, str(e)[:70]))
            time.sleep(10 * (attempt + 1))
            continue
        time.sleep(T.DELAY)
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                time.sleep(10)
                continue
            json.dump(data, open(path, "w", encoding="utf-8"))
            return data
        print("      %s %s %s: HTTP %s" % (season, stype, key, r.status_code))
        time.sleep(10 * (attempt + 1))
    return None


def requests_for(season, stype):
    """(key, endpoint, params, kind) for one season and season type."""
    y = int(season[:4])
    out = []
    if y >= 2015:
        for grouping, types in (("offensive", OFF), ("defensive", DEF)):
            for pt in types:
                out.append(("pt_%s_%s" % (SHORT[pt], grouping[:3]), "synergyplaytypes", {
                    "LeagueID": "00", "PerMode": "Totals", "PlayType": pt, "PlayerOrTeam": "P",
                    "SeasonType": stype, "SeasonYear": season, "TypeGrouping": grouping}, "pt"))
        out.append(("hustle", "leaguehustlestatsplayer",
                    dict(T.BASE_PT, Season=season, SeasonType=stype), "def"))
    if y >= 2013:
        for key, cat in DEFEND:
            out.append(("dfd_" + key, "leaguedashptdefend",
                        dict(T.BASE_PT, DefenseCategory=cat, Season=season, SeasonType=stype), "def"))
    return out


def player_col(headers):
    for c in ("PLAYER_ID", "CLOSE_DEF_PERSON_ID"):
        if c in headers:
            return headers.index(c)
    return None


def long_rows(key, kind, headers, rows):
    """{nba_id: {metric: value}} — play types summed across a player's teams."""
    pi = player_col(headers)
    out = defaultdict(lambda: defaultdict(float))
    if pi is None:
        return out
    h = [x.upper() for x in headers]
    for r in rows:
        pid = int(r[pi])
        d = dict(zip(h, r))
        if kind == "pt":
            poss = float(d.get("POSS") or 0)
            m = out[pid]
            m[key + "_poss"] += poss
            for c in ("PTS", "FGM", "FGA", "FGMX"):
                if d.get(c) is not None:
                    m[key + "_" + c.lower()] += float(d[c])
            # rates are per possession; keep the counts so teams add up
            for c, name in (("TOV_POSS_PCT", "tov"), ("SF_POSS_PCT", "sf"),
                            ("PLUSONE_POSS_PCT", "and1"), ("SCORE_POSS_PCT", "score"),
                            ("FT_POSS_PCT", "ftposs")):
                if d.get(c) is not None:
                    m[key + "_" + name] += float(d[c]) * poss
        else:
            for c, v in d.items():
                if c in SKIP or v is None or isinstance(v, str):
                    continue
                out[pid][key + "_" + c.lower()] = float(v)
    return out


def season_data(season, force):
    """{(stype, kind): {nba_id: {metric: value}}}"""
    got = {}
    for stype in TYPES:
        for key, endpoint, params, kind in requests_for(season, stype):
            data = fetch(key, endpoint, params, season, stype, force)
            headers, rows = T.table_of(data)
            for pid, metrics in long_rows(key, kind, headers, rows).items():
                got.setdefault((TYPES[stype], kind), {}).setdefault(pid, {}).update(metrics)
    return got


def cmd_check(season):
    force = season == T.current_season()
    for key, endpoint, params, kind in requests_for(season, "Regular Season"):
        data = fetch(key, endpoint, params, season, "Regular Season", force)
        headers, rows = T.table_of(data)
        cols = [c.lower() for c in headers if c.upper() not in SKIP]
        print("  %-16s %4d rows  %s" % (key, len(rows), ", ".join(cols)[:140]))


def cmd_build(seasons):
    import duckdb
    env = T.load_env()
    import nightly2 as N
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    con = duckdb.connect(DB_PATH, read_only=True)
    ids = {int(s): p for s, p in con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall()
        if str(s).isdigit()}
    con.close()
    out = duckdb.connect()
    cur = T.current_season()
    for season in seasons:
        got = season_data(season, season == cur)
        for kind, fname in (("pt", "playtypes"), ("def", "defense")):
            rows = []
            for (stype, k), players in got.items():
                if k != kind:
                    continue
                for pid, m in players.items():
                    rows.append(dict(m, nba_person_id=pid, player_id=ids.get(pid),
                                     season=season, season_type=stype))
            if not rows:
                continue
            cols = sorted({c for r in rows for c in r})
            with tempfile.TemporaryDirectory() as tmp:
                jl = os.path.join(tmp, "r.jsonl")
                with open(jl, "w") as f:
                    for r in rows:
                        f.write(json.dumps({c: r.get(c) for c in cols}) + "\n")
                pq = os.path.join(tmp, "o.parquet")
                out.execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                            "TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (jl, pq))
                s3.upload_file(pq, bucket, "v2/seasons/%s/%s.parquet" % (season, fname))
            unmatched = sum(1 for r in rows if not r["player_id"])
            print("  %s %-9s %4d player rows, %3d metrics, %d without our id"
                  % (season, fname, len(rows), len(cols) - 5, unmatched))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season or "2024-25")
        return
    if a.all:
        seasons = [T.label(y) for y in range(2013, int(T.current_season()[:4]) + 1)]
    elif a.season:
        seasons = [a.season]
    else:
        sys.exit("  give --season or --all")
    cmd_build(seasons)


if __name__ == "__main__":
    main()
