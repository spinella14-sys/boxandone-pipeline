#!/usr/bin/env python3
"""
ingest_schedule.py — the full season schedule, preseason included.

Basketball Reference only publishes a box score once a game is finished, which
is why the pipeline has no idea a game exists until the next morning. For
taking notes during a game that is not good enough, and preseason is not on
Basketball Reference at all.

NBA.com publishes the whole schedule — preseason, regular season, the Cup,
play-in and playoffs — as one static JSON file, updated as games are added and
rescheduled. One request gets everything.

Season type comes from the game id rather than a label: NBA ids are
00TYYSSSSS, where the third digit is the type.

    1  preseason        3  all-star
    2  regular season   4  playoffs      5  play-in

    python3 ingest_schedule.py check          # fetch and summarise, write nothing
    python3 ingest_schedule.py build          # store to duckdb and R2
    python3 ingest_schedule.py upcoming       # the next week, as the app will see it

Writes a `scheduled_games` table and v2/schedule/{season}.parquet, which the
game-note picker reads.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")
CACHE = os.path.join(HOME, "raw", "schedule")
URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
PREFIX = "v2/schedule"

GAME_TYPE = {"1": "preseason", "2": "regular", "3": "allstar",
             "4": "playoff", "5": "playin"}

DDL = """
CREATE TABLE IF NOT EXISTS scheduled_games (
    nba_game_id  VARCHAR PRIMARY KEY,
    season       VARCHAR,
    season_type  VARCHAR,
    game_date    DATE,
    game_time_et VARCHAR,
    home_abbr    VARCHAR,
    away_abbr    VARCHAR,
    home_name    VARCHAR,
    away_name    VARCHAR,
    arena        VARCHAR,
    status       VARCHAR,
    home_score   INTEGER,
    away_score   INTEGER
)
"""


def load_env():
    if not os.path.exists(ENV_PATH):
        return {}
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# The CDN rejects requests that do not look like they came from nba.com, even
# with a convincing TLS fingerprint. Chrome impersonation alone returns 403 on
# every target; adding these two headers returns the file. The play-by-play
# endpoint happens not to care, which is why this only surfaced here.
NBA_HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(use_cache=True):
    """NBA.com fingerprints plain HTTP clients, so this uses the same Chrome
    impersonation the play-by-play fetch relies on, plus the headers above."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "league.json")
    if use_cache and os.path.exists(path):
        age = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
        if age < 12:
            return json.load(open(path, encoding="utf-8")), True
    try:
        from curl_cffi import requests as creq
        r = creq.get(URL, impersonate="chrome", headers=NBA_HEADERS, timeout=60)
    except ImportError:
        import requests
        r = requests.get(URL, timeout=60, headers=dict(NBA_HEADERS, **{
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}))
    if r.status_code != 200:
        sys.exit("  HTTP %s from NBA.com — the CDN wants Referer and Origin set"
                 % r.status_code)
    data = r.json()
    json.dump(data, open(path, "w", encoding="utf-8"))
    return data, False


def season_label(game_id, date):
    """NBA ids carry a two-digit season: 0022600001 is 2026-27."""
    try:
        yy = int(game_id[3:5])
    except (ValueError, IndexError):
        yy = None
    if yy is None:
        y = date.year if date.month >= 10 else date.year - 1
        return "%d-%s" % (y, str(y + 1)[2:])
    start = 2000 + yy
    return "%d-%s" % (start, str(start + 1)[2:])


def parse(data):
    sched = data.get("leagueSchedule") or {}
    out = []
    for day in sched.get("gameDates") or []:
        for g in day.get("games") or []:
            gid = g.get("gameId") or ""
            raw_date = g.get("gameDateEst") or day.get("gameDate") or ""
            try:
                d = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
            except Exception:
                try:
                    d = datetime.strptime(raw_date.split(" ")[0], "%m/%d/%Y %H:%M:%S").date()
                except Exception:
                    continue
            home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            out.append({
                "nba_game_id": gid,
                "season": season_label(gid, d),
                "season_type": GAME_TYPE.get(gid[2:3], "other") if len(gid) > 2 else "other",
                "game_date": d,
                "game_time_et": (g.get("gameStatusText") or "").strip(),
                "home_abbr": home.get("teamTricode"),
                "away_abbr": away.get("teamTricode"),
                "home_name": "%s %s" % (home.get("teamCity") or "", home.get("teamName") or ""),
                "away_name": "%s %s" % (away.get("teamCity") or "", away.get("teamName") or ""),
                "arena": g.get("arenaName"),
                "status": {1: "scheduled", 2: "live", 3: "final"}.get(g.get("gameStatus"), "?"),
                "home_score": home.get("score"),
                "away_score": away.get("score"),
            })
    return out


def cmd_check():
    data, cached = fetch()
    games = parse(data)
    print("  %s, %d games\n" % ("from cache" if cached else "fetched", len(games)))
    by = {}
    for g in games:
        by.setdefault((g["season"], g["season_type"]), []).append(g)
    print("  %-9s %-11s %6s  %s" % ("season", "type", "games", "date range"))
    for k in sorted(by):
        rows = by[k]
        ds = sorted(r["game_date"] for r in rows)
        print("  %-9s %-11s %6d  %s .. %s" % (k[0], k[1], len(rows), ds[0], ds[-1]))

    pre = [g for g in games if g["season_type"] == "preseason"]
    if pre:
        print("\n  preseason, first six:")
        for g in sorted(pre, key=lambda x: x["game_date"])[:6]:
            print("    %s  %-4s at %-4s  %-22s %s"
                  % (g["game_date"], g["away_abbr"], g["home_abbr"],
                     (g["arena"] or "")[:22], g["game_time_et"]))

    today = datetime.now().date()
    soon = [g for g in games if today <= g["game_date"] <= today + timedelta(days=10)]
    print("\n  %d games in the next ten days" % len(soon))


def cmd_build(env):
    import duckdb
    data, _ = fetch(use_cache=False)
    games = parse(data)
    if not games:
        sys.exit("  nothing parsed")

    con = duckdb.connect(DB_PATH)
    con.execute(DDL)
    con.execute("DELETE FROM scheduled_games")
    cols = ["nba_game_id", "season", "season_type", "game_date", "game_time_et",
            "home_abbr", "away_abbr", "home_name", "away_name", "arena",
            "status", "home_score", "away_score"]
    con.executemany("INSERT INTO scheduled_games VALUES (%s)" % ",".join("?" * len(cols)),
                    [tuple(g[c] for c in cols) for g in games])
    con.commit()
    print("  %d games stored" % len(games))

    if not env.get("R2_BUCKET_NAME"):
        print("  no R2 credentials — skipping export")
        con.close()
        return
    import boto3
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    seasons = [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM scheduled_games ORDER BY season DESC").fetchall()]
    with tempfile.TemporaryDirectory() as tmp:
        for s in seasons:
            p = os.path.join(tmp, "s.parquet")
            if os.path.exists(p):
                os.remove(p)
            con.execute("""COPY (SELECT * FROM scheduled_games WHERE season=?)
                           TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % p, [s])
            s3.upload_file(p, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, s))
            n = con.execute("SELECT COUNT(*) FROM scheduled_games WHERE season=?",
                            [s]).fetchone()[0]
            print("  %-9s %5d games  %4.0f KB" % (s, n, os.path.getsize(p) / 1024))
    con.close()


def cmd_upcoming(days):
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        rows = con.execute("""
            SELECT game_date, season_type, away_abbr, home_abbr, game_time_et, arena
            FROM scheduled_games
            WHERE game_date BETWEEN CURRENT_DATE AND CURRENT_DATE + ?
            ORDER BY game_date, home_abbr""", [days]).fetchall()
    except Exception:
        print("  no schedule table — run build first")
        return
    if not rows:
        print("  nothing scheduled in the next %d days" % days)
        return
    last = None
    for d, st, away, home, t, arena in rows:
        if d != last:
            print("\n  %s" % d)
            last = d
        print("    %-10s %-4s at %-4s  %-18s %s"
              % (st, away, home, (t or "")[:18], (arena or "")[:28]))
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "upcoming"])
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check()
    elif a.cmd == "build":
        cmd_build(load_env())
    else:
        cmd_upcoming(a.days)
