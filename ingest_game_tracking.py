#!/usr/bin/env python3
"""
ingest_game_tracking.py — per-game tracking and matchups from NBA.com.

Four box scores per game, each one request (stats.nba.com, so laptop only):

  track     boxscoreplayertrackv3   distance, speed, touches, passes, potential
                                    and secondary assists, rebound chances,
                                    contested and uncontested shots
  hustle    boxscorehustlev2        contests, deflections, charges, screen
                                    assists, loose balls, box-outs
  defense   boxscoredefensivev2     as the defender: matchup minutes, partial
                                    possessions, points and shots allowed,
                                    switches
  matchups  boxscorematchupsv3      every offensive player against every
                                    defender who guarded him: minutes,
                                    partial possessions, points, shots,
                                    assists, turnovers

NBA.com started these in different seasons; a game that comes back empty is
noted and not asked again. Every numeric field the feed returns is kept, so
nothing is lost to a guessed column list.

Writes
  v2/tracking/{season}/players_game.parquet    one row per player-game
  v2/tracking/{season}/matchups_game.parquet   one row per offense-defender pair
  v2/games/{game_id}/tracking.parquet          for the game page
  v2/games/{game_id}/matchups.parquet

Raw responses are cached under raw/game_tracking/, so a stopped run resumes.
No database lock: it only reads the id mappings.

    python3 ingest_game_tracking.py check --game 0022400061      # show what one game returns
    python3 ingest_game_tracking.py fetch --seasons 2024-25 --minutes 60
    python3 ingest_game_tracking.py build --seasons 2024-25
    python3 ingest_game_tracking.py status
    python3 ingest_game_tracking.py                # the laptop's hourly slice:
                                                   # new games, then backfill
"""
import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
RAW = os.path.join(HOME, "raw", "game_tracking")
OUT = os.path.join(HOME, "data", "game_tracking")
ABSENT = os.path.join(RAW, "absent.json")
DELAY = 1.2

KINDS = {   # kind: (endpoint, first season it can exist)
    "track": ("boxscoreplayertrackv3", 2013),
    "hustle": ("boxscorehustlev2", 2015),
    "defense": ("boxscoredefensivev2", 2017),
    "matchups": ("boxscorematchupsv3", 2017),
}
FIRST = 2013
STYPE = {"001": "preseason", "002": "regular", "004": "playoff", "005": "playin", "006": "cup"}


def label(y):
    return "%d-%s" % (y, str(y + 1)[2:])


def current_season():
    d = date.today()
    return label(d.year if d.month >= 10 else d.year - 1)


def seasons_from(spec):
    lo, hi = spec.split(":") if ":" in spec else (spec, spec)
    a, b = int(lo[:4]), int(hi[:4])
    return [label(y) for y in range(max(b, a), min(a, b) - 1, -1)]     # newest first


# ------------------------------------------------------------------ fetching

def raw_path(kind, gid):
    return os.path.join(RAW, kind, gid[3:5], "%s.json.gz" % gid)


def load_absent():
    try:
        return json.load(open(ABSENT))
    except (OSError, ValueError):
        return {}


def save_absent(a):
    os.makedirs(RAW, exist_ok=True)
    json.dump(a, open(ABSENT, "w"))


def payload(data):
    """The {homeTeam, awayTeam} block, whatever the response calls it."""
    for v in (data or {}).values():
        if isinstance(v, dict) and "homeTeam" in v:
            return v
    return None


def has_players(data):
    p = payload(data)
    return bool(p and ((p.get("homeTeam") or {}).get("players")
                       or (p.get("awayTeam") or {}).get("players")))


def get(kind, gid):
    from curl_cffi import requests as cr
    import ingest_tracking as T
    endpoint = KINDS[kind][0]
    params = {"GameID": gid, "LeagueID": "00", "endPeriod": 0, "endRange": 28800,
              "rangeType": 0, "startPeriod": 0, "startRange": 0}
    for attempt in range(3):
        try:
            r = cr.get("https://stats.nba.com/stats/%s" % endpoint, params=params,
                       headers=T.HEADERS, impersonate="chrome", timeout=40)
        except Exception as e:
            print("      %s %s: %s" % (kind, gid, str(e)[:60]))
            time.sleep(8 * (attempt + 1))
            continue
        time.sleep(DELAY)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                time.sleep(5)
                continue
        if r.status_code in (400, 404):
            return {}
        print("      %s %s: HTTP %s" % (kind, gid, r.status_code))
        time.sleep(8 * (attempt + 1))
    return None          # network trouble: not marked absent, tried again later


def fetch_game(gid, absent, today_season):
    """Fetch whichever of the four this game still lacks. Returns requests made."""
    y = int(gid[3:5])
    year = 2000 + y if y < 90 else 1900 + y
    n = 0
    for kind, (_, first) in KINDS.items():
        if year < first:
            continue
        path = raw_path(kind, gid)
        if os.path.exists(path) or "%s/%s" % (kind, gid) in absent:
            continue
        data = get(kind, gid)
        n += 1
        if data is None:
            continue
        if not has_players(data):
            # empty: older than NBA.com's coverage, or not posted yet. A game of
            # the season under way is asked again; anything older is not.
            if label(year) != today_season:
                absent["%s/%s" % (kind, gid)] = 1
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f)
    return n


def season_games(season, s3=None, bucket=None):
    """[(nba_gid, our_gid, game_date)] for a season, from the play-by-play log
    (hot database for the season under way, the R2 archive for the rest)."""
    import duckdb
    rows = []
    con = duckdb.connect()
    if season == current_season() and os.path.exists(DB_PATH):
        con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
        rows = con.execute("""SELECT nba_game_id, game_id, CAST(game_date AS VARCHAR) FROM hot.pbp_log
                              WHERE season=? AND nba_game_id IS NOT NULL""", [season]).fetchall()
        con.execute("DETACH hot")
    else:
        local = os.path.join(OUT, "games_%s.parquet" % season)
        if not os.path.exists(local) and s3:
            os.makedirs(OUT, exist_ok=True)
            for t in ("pbp_log", "game_identifiers"):
                try:
                    s3.download_file(bucket, "v2/archive/%s/%s.parquet" % (season, t), local + ".tmp")
                except Exception:
                    continue
                col = "nba_game_id" if t == "pbp_log" else "source_game_id"
                where = "" if t == "pbp_log" else "WHERE source='nba'"
                con.execute("COPY (SELECT %s AS nba_game_id, game_id, CAST(game_date AS VARCHAR) AS game_date "
                            "FROM read_parquet('%s') %s) TO '%s' (FORMAT PARQUET)"
                            % (col, local + ".tmp", where, local))
                os.remove(local + ".tmp")
                break
        if os.path.exists(local):
            rows = con.execute("SELECT * FROM read_parquet('%s') WHERE nba_game_id IS NOT NULL" % local).fetchall()
    con.close()
    today = date.today().isoformat()
    return sorted([r for r in rows if (r[2] or "") < today], key=lambda r: r[2], reverse=True)


def r2():
    import nightly2 as N
    env = N.load_env()
    return N.s3c(env), env["R2_BUCKET_NAME"]


def cmd_fetch(seasons, minutes):
    s3, bucket = r2()
    absent = load_absent()
    stop = time.time() + minutes * 60 if minutes else None
    cur = current_season()
    touched = []
    for season in seasons:
        if int(season[:4]) < FIRST:
            continue
        games = season_games(season, s3, bucket)
        todo = [g for g in games if any(
            not os.path.exists(raw_path(k, g[0])) and "%s/%s" % (k, g[0]) not in absent
            and int(season[:4]) >= KINDS[k][1] for k in KINDS)]
        if not todo:
            continue
        print("  %s: %d of %d games to fetch" % (season, len(todo), len(games)))
        done = 0
        for nba, ours, d in todo:
            if stop and time.time() > stop:
                break
            fetch_game(nba, absent, cur)
            done += 1
            if done % 50 == 0:
                save_absent(absent)
                print("      %d/%d" % (done, len(todo)))
        save_absent(absent)
        touched.append(season)
        print("      fetched %d games" % done)
        if stop and time.time() > stop:
            print("  time's up — the next run carries on from here")
            break
    return touched


# ------------------------------------------------------------------ parsing

def number(k, v):
    """Numbers stay numbers; clock strings ("12:34", "PT12M34.00S") become seconds."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and "min" in k.lower():
        m = re.match(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$", v)
        if m:
            return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
        m = re.match(r"^(\d+):(\d+(?:\.\d+)?)$", v)
        if m:
            return int(m.group(1)) * 60 + float(m.group(2))
    return None


def snake(k):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()


def stats_of(d, prefix):
    out = {}
    for k, v in (d or {}).items():
        x = number(k, v)
        if x is not None:
            out["%s_%s" % (prefix, snake(k))] = x
    return out


def parse_game(nba, ours, gdate, season, ids):
    players, pairs = {}, []
    for kind in KINDS:
        path = raw_path(kind, nba)
        if not os.path.exists(path):
            continue
        p = payload(json.load(gzip.open(path, "rt", encoding="utf-8")))
        if not p:
            continue
        for side in ("homeTeam", "awayTeam"):
            team = p.get(side) or {}
            for pl in team.get("players") or []:
                pid = pl.get("personId")
                if pid is None:
                    continue
                if kind != "matchups":
                    row = players.setdefault(pid, {
                        "season": season, "game_id": ours, "nba_game_id": nba, "game_date": gdate,
                        "season_type": STYPE.get(nba[:3], "other"), "side": side[:4],
                        "team": team.get("teamTricode"), "nba_person_id": pid,
                        "player_id": ids.get(pid)})
                    row.update(stats_of(pl.get("statistics"), kind))
                    continue
                for m in pl.get("matchups") or []:
                    did = m.get("personId")
                    row = {"season": season, "game_id": ours, "nba_game_id": nba, "game_date": gdate,
                           "season_type": STYPE.get(nba[:3], "other"),
                           "off_team": team.get("teamTricode"), "off_nba_id": pid,
                           "off_player_id": ids.get(pid), "def_nba_id": did,
                           "def_player_id": ids.get(did)}
                    row.update(stats_of(m.get("statistics"), "m"))
                    pairs.append(row)
    return list(players.values()), pairs


def write(con, rows, path):
    if not rows:
        return False
    cols = sorted({c for r in rows for c in r})
    with tempfile.TemporaryDirectory() as tmp:
        jl = os.path.join(tmp, "r.jsonl")
        with open(jl, "w") as f:
            for r in rows:
                f.write(json.dumps({c: r.get(c) for c in cols}) + "\n")
        if os.path.exists(path):
            os.remove(path)
        con.execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited', "
                    "sample_size=-1)) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (jl, path))
    return True


def player_ids():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    ids = {int(s): p for s, p in con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall()
        if str(s).isdigit()}
    con.close()
    return ids


def cmd_build(seasons, only_games=None):
    """only_games: NBA ids whose per-game files to (re)write; None writes all."""
    import duckdb
    from concurrent.futures import ThreadPoolExecutor
    s3, bucket = r2()
    ids = player_ids()
    con = duckdb.connect()
    os.makedirs(OUT, exist_ok=True)
    for season in seasons:
        games = season_games(season, s3, bucket)
        allp, allm, per_game = [], [], []
        for nba, ours, d in games:
            p, m = parse_game(nba, ours, d, season, ids)
            allp += p
            allm += m
            if (p or m) and (only_games is None or nba in only_games):
                per_game.append((ours, p, m))
        if not allp and not allm:
            continue
        for rows, name in ((allp, "players_game"), (allm, "matchups_game")):
            path = os.path.join(OUT, "%s_%s.parquet" % (season, name))
            if write(con, rows, path):
                s3.upload_file(path, bucket, "v2/tracking/%s/%s.parquet" % (season, name))

        def one(job):
            ours, p, m = job
            c = duckdb.connect()
            with tempfile.TemporaryDirectory() as tmp:
                for rows, name in ((p, "tracking"), (m, "matchups")):
                    path = os.path.join(tmp, name + ".parquet")
                    if write(c, rows, path):
                        s3.upload_file(path, bucket, "v2/games/%s/%s.parquet" % (ours, name))
            c.close()
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(one, per_game))
        gp = len({r["game_id"] for r in allp})
        unmatched = sum(1 for r in allp if not r["player_id"])
        print("  %s: %d games with tracking, %d player-games, %d matchup pairs, "
              "%d game pages written, %d player-games without our id"
              % (season, gp, len(allp), len(allm), len(per_game), unmatched))


# ------------------------------------------------------------------ commands

def cmd_check(gid):
    absent = {}
    for kind in KINDS:
        path = raw_path(kind, gid)
        if os.path.exists(path):
            os.remove(path)
    fetch_game(gid, absent, "")
    for kind in KINDS:
        path = raw_path(kind, gid)
        if not os.path.exists(path):
            print("  %-9s nothing returned" % kind)
            continue
        p = payload(json.load(gzip.open(path, "rt", encoding="utf-8")))
        pl = ((p.get("homeTeam") or {}).get("players") or [{}])[0]
        st = pl.get("statistics") or {}
        print("  %-9s %d+%d players; fields: %s" % (
            kind, len((p.get("homeTeam") or {}).get("players") or []),
            len((p.get("awayTeam") or {}).get("players") or []), ", ".join(sorted(st))[:200]))
        if kind == "matchups":
            m = (pl.get("matchups") or [{}])[0]
            print("            first player has %d matchups; fields: %s"
                  % (len(pl.get("matchups") or []), ", ".join(sorted(m.get("statistics") or {}))[:200]))


def cmd_status():
    absent = load_absent()
    print("  %-9s  %s" % ("season", "  ".join("%8s" % k for k in KINDS)))
    for season in seasons_from("%s:%d" % (current_season(), FIRST)):
        n = []
        for k in KINDS:
            d = os.path.join(RAW, k, season[2:4])
            n.append(len(os.listdir(d)) if os.path.isdir(d) else 0)
        print("  %-9s  %s" % (season, "  ".join("%8d" % x for x in n)))
    print("\n  %d requests came back empty (before NBA.com's coverage) and are not retried"
          % len(absent))


def stats_up():
    import nightly2 as N
    return N.stats_reachable(lambda m: None)


def cmd_daily(minutes):
    """The laptop's hourly slice: the season under way, then the backfill."""
    if not stats_up():
        print("  stats.nba.com unreachable — nothing to do here")
        return
    cur = current_season()
    before = {g[0] for g in season_games(cur) if os.path.exists(raw_path("track", g[0]))}
    cmd_fetch([cur], None)
    after = {g[0] for g in season_games(cur) if os.path.exists(raw_path("track", g[0]))}
    cmd_build([cur], only_games=after - before)
    left = max(0, minutes - 2)
    if left:
        older = seasons_from("%d:%d" % (int(cur[:4]) - 1, FIRST))
        touched = cmd_fetch(older, left)
        if touched:
            cmd_build(touched)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="daily",
                    choices=["daily", "check", "fetch", "build", "status"])
    ap.add_argument("--game")
    ap.add_argument("--seasons")
    ap.add_argument("--minutes", type=float, default=12)
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.game or "0022400061")
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "daily":
        cmd_daily(a.minutes)
    else:
        seasons = seasons_from(a.seasons or current_season())
        if a.cmd == "fetch":
            touched = cmd_fetch(seasons, a.minutes if a.minutes > 0 else None)
            if touched:
                cmd_build(touched)
        else:
            cmd_build(seasons)


if __name__ == "__main__":
    main()
