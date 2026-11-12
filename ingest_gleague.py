#!/usr/bin/env python3
"""
ingest_gleague.py — the G League: every game, box score and shot.

Source: NBA's stats site with the G League's league code (LeagueID=20). The
Mac reaches it directly; GitHub cannot, so on GitHub (or with --relay) every
request goes through the Cloudflare Worker named in NBA_RELAY_URL, carrying
NBA_RELAY_TOKEN.

What it keeps, in the working database, kept apart from the NBA's tables so
no NBA total can ever pick up a G League game:

  gl_teams         every G League team by season (id, code, city, name)
  gl_games         every game: date, teams, score, status, season type, label
  gl_player_game   one row per player per game, the box score line
                   (player_id is ours once the person is in the registry;
                    person_id is NBA.com's, the same id system as the NBA)

and, outside the database because there are ~180,000 a season:

  data/gleague/shots/{season}.parquet   every field-goal attempt with its
                                        location, from the play-by-play

Players: NBA.com uses one person id across the NBA and the G League, so a G
League player who has played in the NBA is already in our registry. Anyone
new is added (or, if his name matches someone we cannot tell him apart from,
staged for review), exactly as the NBA rosters do it.

Season types: kept per game (regular, tip-off, showcase, playoff…), but the
season files combine them all — a G League season is shown whole. Preseason
and all-star games are left out of the season totals.

    python3 ingest_gleague.py players --sync              # the all-time player list
    python3 ingest_gleague.py run --season 2026-27 --sync # schedule, fetch, parse, export
    python3 ingest_gleague.py download --seasons 2005-06:2025-26   # hours; no lock; resumable
    python3 ingest_gleague.py backfill --seasons 2005-06:2025-26 --sync   # then: short, locked

  --sync (on the Mac) takes the nightly's lock, pulls the working database
  from R2, writes, and pushes it back, so the nightly never overwrites the
  work. The nightly calls without it — it holds the lock and the database.
    python3 ingest_gleague.py sample --game 2052600001    # print a raw response's shape
    python3 ingest_gleague.py check --season 2026-27      # box totals against the final scores
"""
import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

DATA = os.path.join(HOME, "data")
RAW = os.path.join(HOME, "raw", "gleague")
SHOTS = os.path.join(DATA, "gleague", "shots")
PREFIX = "v2"
LEAGUE = "20"

HEADERS = {
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}
DELAY = 0.6             # between requests: polite, and the Worker is per-request anyway

# NBA game ids carry the season type in their first three digits
TYPES = {"201": "preseason", "202": "regular", "203": "allstar", "204": "playoff",
         "205": "tipoff", "206": "showcase", "207": "showcase"}
# left out of season totals (still stored)
NOT_COUNTED = ("preseason", "allstar")

DDL = """
CREATE TABLE IF NOT EXISTS gl_teams (
    team_id BIGINT, season VARCHAR, abbr VARCHAR, city VARCHAR, name VARCHAR,
    PRIMARY KEY (team_id, season));
CREATE TABLE IF NOT EXISTS gl_games (
    game_id VARCHAR PRIMARY KEY, season VARCHAR, season_type VARCHAR, label VARCHAR,
    game_date DATE, home_id BIGINT, away_id BIGINT, home_abbr VARCHAR, away_abbr VARCHAR,
    home_score INTEGER, away_score INTEGER, status INTEGER,
    fetched BOOLEAN DEFAULT FALSE, parsed BOOLEAN DEFAULT FALSE, shots INTEGER,
    error VARCHAR, updated_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS gl_player_game (
    person_id VARCHAR, game_id VARCHAR, player_id VARCHAR, name VARCHAR,
    season VARCHAR, season_type VARCHAR, game_date DATE,
    team_id BIGINT, team_abbr VARCHAR, opp_abbr VARCHAR, is_home BOOLEAN,
    started BOOLEAN, played BOOLEAN, dnp_reason VARCHAR, seconds_played INTEGER,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    orb INTEGER, drb INTEGER, trb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER,
    tov INTEGER, pf INTEGER, pts INTEGER, plus_minus INTEGER,
    PRIMARY KEY (person_id, game_id));
"""


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------

def use_relay(force=False):
    return bool(os.environ.get("NBA_RELAY_URL")) and (force or os.environ.get("GITHUB_ACTIONS"))


def stats_get(endpoint, params, relay=False, tries=4):
    """One stats.nba.com call, directly or through the Worker. JSON or None."""
    from urllib.parse import urlencode
    from curl_cffi import requests as cr
    q = urlencode(params)
    if use_relay(relay):
        url = "%s/stats/%s?%s" % (os.environ["NBA_RELAY_URL"].rstrip("/"), endpoint, q)
        headers = {"x-relay-token": os.environ.get("NBA_RELAY_TOKEN", "")}
    else:
        url = "https://stats.nba.com/stats/%s?%s" % (endpoint, q)
        headers = HEADERS
    for attempt in range(tries):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s: %s" % (endpoint, str(e)[:70]))
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code in (400, 404):
            return None
        print("      %s: HTTP %s" % (endpoint, r.status_code))
        time.sleep(10 * (attempt + 1))
    return None


def rows_of(j, name=None):
    for s in (j or {}).get("resultSets") or []:
        if name is None or s.get("name") == name:
            return [dict(zip(s["headers"], r)) for r in s.get("rowSet") or []]
    return []


def cache_path(season, gid, kind):
    return os.path.join(RAW, season, "%s_%s.json.gz" % (gid, kind))


def read_cache(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_cache(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f)


def db():
    import duckdb
    con = duckdb.connect(os.path.join(DATA, "boxandone.duckdb"))
    for stmt in DDL.split(";"):
        if stmt.strip():
            con.execute(stmt)
    return con


def season_list(spec):
    lo, hi = spec.split(":")
    return ["%d-%02d" % (y, (y + 1) % 100) for y in range(int(lo[:4]), int(hi[:4]) + 1)]


def s3_client():
    import nightly2 as N
    env = N.load_env()
    return N.s3c(env), env["R2_BUCKET_NAME"]


# ---------------------------------------------------------------------------
# players
# ---------------------------------------------------------------------------

def cmd_players(current_only=False, relay=False, from_boxes=False):
    """Bring every G League player into the registry by NBA.com person id —
    from NBA.com's list, or (from_boxes) from box lines that have no player."""
    import ingest_rosters as R
    season = "2026-27"
    people = []
    if from_boxes:
        con = db()
        try:
            rows = con.execute("""
                SELECT person_id, arg_max(name, game_date), arg_max(team_abbr, game_date)
                FROM gl_player_game WHERE player_id IS NULL AND person_id IS NOT NULL
                  AND person_id NOT IN ('0', 'None')
                GROUP BY person_id""").fetchall()
        finally:
            con.close()
        for pid, name, team in rows:
            if name and name.strip():
                people.append({"nba_id": str(pid), "name": name, "name_norm": R.norm(name),
                               "team_abbr": team, "birthdate": None, "height_in": None, "school": None})
        j = None
    else:
        j = stats_get("commonallplayers", {"IsOnlyCurrentSeason": 1 if current_only else 0,
                                           "LeagueID": LEAGUE, "Season": season}, relay)
    for r in rows_of(j):
        name = r.get("DISPLAY_FIRST_LAST") or ""
        if not r.get("PERSON_ID") or not name.strip():
            continue
        people.append({"nba_id": str(r["PERSON_ID"]), "name": name, "name_norm": R.norm(name),
                       "team_abbr": r.get("TEAM_ABBREVIATION") or None, "birthdate": None,
                       "height_in": None, "school": None})
    if not people:
        print("  no players came back — nothing changed")
        return
    con = db()
    try:
        decided = R.resolve(con, people)
        counts = {}
        for d in decided:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        creating = [d for d in decided if d["decision"] == "create"]
        for d, pid in zip(creating, R.next_ids(con, len(creating))):
            d["player_id"] = pid
            con.execute("""
                INSERT INTO players (player_id, full_name, display_name, name_normalized,
                       birthdate, birthdate_status, status, provenance, created_at,
                       updated_at, created_by)
                VALUES (?, ?, ?, ?, NULL, 'missing', 'active', 'scraped',
                        current_timestamp, current_timestamp, 'ingest_gleague')""",
                [pid, d["name"], d["name"], d["name_norm"]])
        for d in decided:
            if d["decision"] in ("create", "link"):
                con.execute("INSERT INTO player_identifiers (player_id, source, source_id) "
                            "VALUES (?, 'nba', ?) ON CONFLICT DO NOTHING", [d["player_id"], d["nba_id"]])
                con.execute("""INSERT INTO player_aliases (player_id, alias, alias_normalized, source, first_seen)
                               VALUES (?, ?, ?, 'nba', current_timestamp) ON CONFLICT DO NOTHING""",
                            [d["player_id"], d["name"], d["name_norm"]])
            elif d["decision"] == "stage":
                con.execute("""
                    INSERT INTO staging_players (source, source_id, raw_name, raw_name_norm, raw_team,
                           raw_league, first_seen, last_seen, occurrences, match_status,
                           proposed_player_id, match_score, match_reasons)
                    VALUES ('nba', ?, ?, ?, ?, 'G League', current_timestamp, current_timestamp, 1,
                            'pending', ?, ?, ?) ON CONFLICT DO NOTHING""",
                    [d["nba_id"], d["name"], d["name_norm"], d["team_abbr"], d["player_id"],
                     d.get("score"), d.get("why")])
        # box lines already held for these people get their player id
        con.execute("""UPDATE gl_player_game g SET player_id = i.player_id
                       FROM player_identifiers i
                       WHERE i.source = 'nba' AND i.source_id = g.person_id AND g.player_id IS NULL""")
        con.commit()
    finally:
        con.close()
    print("  %d G League players: %d already ours, %d linked, %d new, %d staged for review" % (
        len(people), counts.get("already", 0), counts.get("link", 0),
        counts.get("create", 0), counts.get("stage", 0)))


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

def schedule_rows(season, relay=False):
    """Every game of a season. A finished season's schedule is saved the first
    time it is read and comes from disk after that."""
    import nightly2 as N
    saved = os.path.join(RAW, season, "schedule.json.gz")
    if season != N.current_season() and os.path.exists(saved):
        return read_cache(saved)
    games = _schedule_rows(season, relay)
    if games and season != N.current_season():
        write_cache(saved, games)
    return games


def _schedule_rows(season, relay=False):
    """From the league schedule, or for older seasons the team game logs."""
    j = stats_get("scheduleleaguev2", {"LeagueID": LEAGUE, "Season": season}, relay)
    games = []
    for d in ((j or {}).get("leagueSchedule") or {}).get("gameDates") or []:
        for g in d.get("games") or []:
            h, a = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            gd = (g.get("gameDateEst") or g.get("gameDate") or "")[:10]
            label = " ".join(x for x in (g.get("gameLabel"), g.get("gameSubLabel")) if x) or None
            games.append({
                "game_id": str(g["gameId"]), "game_date": gd, "status": g.get("gameStatus"),
                "label": label,
                "home": (h.get("teamId"), h.get("teamTricode"), h.get("teamCity"), h.get("teamName"), h.get("score")),
                "away": (a.get("teamId"), a.get("teamTricode"), a.get("teamCity"), a.get("teamName"), a.get("score")),
            })
    if games:
        return games
    # older seasons: one row per team per game in the game log
    out = {}
    for stype in ("Regular Season", "Playoffs"):
        rows = rows_of(stats_get("leaguegamelog", {
            "Counter": 0, "Direction": "ASC", "LeagueID": LEAGUE, "PlayerOrTeam": "T",
            "Season": season, "SeasonType": stype, "Sorter": "DATE"}, relay))
        for r in rows:
            gid = str(r["GAME_ID"])
            home = " vs. " in (r.get("MATCHUP") or "")
            g = out.setdefault(gid, {"game_id": gid, "game_date": str(r["GAME_DATE"])[:10],
                                     "status": 3, "label": None, "home": None, "away": None})
            g["home" if home else "away"] = (r.get("TEAM_ID"), r.get("TEAM_ABBREVIATION"), None,
                                             r.get("TEAM_NAME"), r.get("PTS"))
        time.sleep(DELAY)
    return [g for g in out.values() if g["home"] and g["away"]]


def season_type_of(gid, label):
    t = TYPES.get(gid[:3], "other")
    if label and "all-star" in label.lower():
        return "allstar"
    return t


def cmd_schedule(con, season, relay=False):
    games = schedule_rows(season, relay)
    for g in games:
        stype = season_type_of(g["game_id"], g["label"])
        for side in ("home", "away"):
            tid, abbr, city, name, _ = g[side]
            if tid:
                con.execute("""INSERT INTO gl_teams VALUES (?,?,?,?,?)
                               ON CONFLICT (team_id, season) DO UPDATE SET abbr=excluded.abbr,
                               city=COALESCE(excluded.city, gl_teams.city), name=COALESCE(excluded.name, gl_teams.name)""",
                            [tid, season, abbr, city, name])
        con.execute("""
            INSERT INTO gl_games (game_id, season, season_type, label, game_date, home_id, away_id,
                   home_abbr, away_abbr, home_score, away_score, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, now())
            ON CONFLICT (game_id) DO UPDATE SET
                status = excluded.status, game_date = excluded.game_date, label = excluded.label,
                home_score = excluded.home_score, away_score = excluded.away_score,
                updated_at = now()""",
            [g["game_id"], season, stype, g["label"], g["game_date"] or None, g["home"][0], g["away"][0],
             g["home"][1], g["away"][1], g["home"][4], g["away"][4], g["status"]])
    final = sum(1 for g in games if g["status"] == 3)
    kinds = {}
    for g in games:
        k = season_type_of(g["game_id"], g["label"])
        kinds[k] = kinds.get(k, 0) + 1
    print("  %s schedule: %d games, %d final — %s" % (
        season, len(games), final, ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    return final


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def cmd_fetch(con, season, limit=None, relay=False):
    todo = [r[0] for r in con.execute("""
        SELECT game_id FROM gl_games WHERE season = ? AND status = 3 AND NOT fetched
        ORDER BY game_date, game_id""", [season]).fetchall()]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("  %s: nothing to fetch" % season)
        return 0
    print("  %s: fetching %d games (box score and play-by-play)%s" % (
        season, len(todo), " through the relay" if use_relay(relay) else ""))
    done = failed = 0
    for i, gid in enumerate(todo, 1):
        ok = fetch_game(season, gid, relay)
        if ok:
            con.execute("UPDATE gl_games SET fetched = TRUE, error = NULL WHERE game_id = ?", [gid])
            done += 1
        else:
            con.execute("UPDATE gl_games SET error = 'no box score' WHERE game_id = ?", [gid])
            failed += 1
        if i % 50 == 0:
            con.commit()
            print("    %d/%d" % (i, len(todo)))
    con.commit()
    print("  %s: fetched %d, failed %d" % (season, done, failed))
    return done


def fetch_game(season, gid, relay=False):
    """A game's box score and play-by-play to disk, unless already there. True
    if the box score is on disk afterwards."""
    for kind, ep, params in (
            ("box", "boxscoretraditionalv3", {"GameID": gid, "StartPeriod": 0, "EndPeriod": 0,
                                              "StartRange": 0, "EndRange": 0, "RangeType": 0}),
            ("pbp", "playbyplayv3", {"GameID": gid, "StartPeriod": 0, "EndPeriod": 0})):
        path = cache_path(season, gid, kind)
        if os.path.exists(path):
            continue
        j = stats_get(ep, params, relay)
        time.sleep(DELAY)
        if j is None or (kind == "box" and not (j.get("boxScoreTraditional") or {}).get("homeTeam")):
            continue                              # play-by-play is a bonus; the box score is the game
        write_cache(path, j)
    return os.path.exists(cache_path(season, gid, "box"))


def cmd_download(seasons, relay=False):
    """Every finished game of these seasons to disk. No database, no lock;
    stop and restart freely."""
    for season in seasons:
        games = [g for g in schedule_rows(season, relay) if g["status"] == 3]
        have = sum(1 for g in games if os.path.exists(cache_path(season, g["game_id"], "box")))
        todo = [g["game_id"] for g in games if not os.path.exists(cache_path(season, g["game_id"], "box"))]
        print("  %s: %d finished games, %d already saved, %d to download" % (
            season, len(games), have, len(todo)))
        t0, got, miss = time.time(), 0, 0
        for i, gid in enumerate(todo, 1):
            if fetch_game(season, gid, relay):
                got += 1
            else:
                miss += 1
            if i % 100 == 0:
                rate = (time.time() - t0) / i
                print("    %d/%d  (~%d min left in %s)" % (i, len(todo), rate * (len(todo) - i) / 60, season))
        pbp = sum(1 for g in games if os.path.exists(cache_path(season, g["game_id"], "pbp")))
        print("  %s: %d downloaded, %d with no box score; play-by-play for %d of %d games" % (
            season, got, miss, pbp, len(games)))


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def mmss(v):
    """'34:12' or 'PT34M12.00S' -> seconds."""
    s = str(v or "").strip()
    if not s:
        return None
    if s.startswith("PT"):
        import re
        m = re.match(r"PT(\d+)M([\d.]+)S", s)
        return int(round(int(m.group(1)) * 60 + float(m.group(2)))) if m else None
    if ":" in s:
        a, b = s.split(":")[:2]
        try:
            return int(a) * 60 + int(float(b))
        except ValueError:
            return None
    try:
        return int(float(s) * 60)
    except ValueError:
        return None


def box_lines(j, game):
    """boxscoretraditionalv3 -> one dict per player."""
    b = j.get("boxScoreTraditional") or {}
    out = []
    for side, opp in (("homeTeam", "awayTeam"), ("awayTeam", "homeTeam")):
        t, o = b.get(side) or {}, b.get(opp) or {}
        for p in t.get("players") or []:
            s = p.get("statistics") or {}
            secs = mmss(s.get("minutes"))
            comment = (p.get("comment") or "").strip()
            played = bool(secs) or any(s.get(k) for k in ("fieldGoalsAttempted", "points", "reboundsTotal"))
            name = " ".join(x for x in (p.get("firstName"), p.get("familyName")) if x) or p.get("nameI")
            pm = s.get("plusMinusPoints")
            out.append({
                "person_id": str(p.get("personId")), "name": name,
                "team_id": t.get("teamId"), "team_abbr": t.get("teamTricode"),
                "opp_abbr": o.get("teamTricode"), "is_home": side == "homeTeam",
                "started": bool((p.get("position") or "").strip()), "played": played,
                "dnp_reason": None if played else (comment or "Did Not Play"),
                "seconds_played": secs if played else None,
                "fgm": s.get("fieldGoalsMade"), "fga": s.get("fieldGoalsAttempted"),
                "fg3m": s.get("threePointersMade"), "fg3a": s.get("threePointersAttempted"),
                "ftm": s.get("freeThrowsMade"), "fta": s.get("freeThrowsAttempted"),
                "orb": s.get("reboundsOffensive"), "drb": s.get("reboundsDefensive"),
                "trb": s.get("reboundsTotal"), "ast": s.get("assists"), "stl": s.get("steals"),
                "blk": s.get("blocks"), "tov": s.get("turnovers"), "pf": s.get("foulsPersonal"),
                "pts": s.get("points"),
                "plus_minus": int(round(pm)) if isinstance(pm, (int, float)) else None,
            })
    return out


SHOT_COLS = ["game_id", "season", "season_type", "game_date", "action_number", "period", "clock",
             "person_id", "team_abbr", "x", "y", "distance", "made", "shot_value",
             "action_type", "sub_type", "description"]


def shot_rows(j, game):
    acts = ((j or {}).get("game") or {}).get("actions") or []
    out = []
    for a in acts:
        if a.get("isFieldGoal") != 1:
            continue
        out.append([game["game_id"], game["season"], game["season_type"], game["game_date"],
                    a.get("actionNumber"), a.get("period"), a.get("clock"), str(a.get("personId")),
                    a.get("teamTricode"), a.get("xLegacy"), a.get("yLegacy"), a.get("shotDistance"),
                    a.get("shotResult") == "Made", a.get("shotValue"),
                    a.get("actionType"), a.get("subType"), a.get("description")])
    return out


def cmd_parse(con, season):
    games = con.execute("""
        SELECT game_id, season, season_type, game_date FROM gl_games
        WHERE season = ? AND fetched AND NOT parsed ORDER BY game_date""", [season]).fetchall()
    if not games:
        print("  %s: nothing to parse" % season)
        return []
    ids = dict(con.execute("SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall())
    shots, lines_n, unknown = [], 0, set()
    for gid, s, stype, gdate in games:
        game = {"game_id": gid, "season": s, "season_type": stype, "game_date": gdate}
        try:
            lines = box_lines(read_cache(cache_path(season, gid, "box")), game)
        except Exception as e:
            con.execute("UPDATE gl_games SET error = ? WHERE game_id = ?", [("parse: " + str(e))[:200], gid])
            continue
        con.execute("DELETE FROM gl_player_game WHERE game_id = ?", [gid])
        for p in lines:
            pid = ids.get(p["person_id"])
            if not pid:
                unknown.add((p["person_id"], p["name"]))
            con.execute("""
                INSERT INTO gl_player_game VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [p["person_id"], gid, pid, p["name"], s, stype, gdate, p["team_id"], p["team_abbr"],
                 p["opp_abbr"], p["is_home"], p["started"], p["played"], p["dnp_reason"],
                 p["seconds_played"], p["fgm"], p["fga"], p["fg3m"], p["fg3a"], p["ftm"], p["fta"],
                 p["orb"], p["drb"], p["trb"], p["ast"], p["stl"], p["blk"], p["tov"], p["pf"],
                 p["pts"], p["plus_minus"]])
            lines_n += 1
        n = 0
        pbp = cache_path(season, gid, "pbp")
        if os.path.exists(pbp):
            try:
                rows = shot_rows(read_cache(pbp), game)
                shots += rows
                n = len(rows)
            except Exception:
                pass
        con.execute("UPDATE gl_games SET parsed = TRUE, shots = ?, error = NULL WHERE game_id = ?", [n, gid])
    con.commit()
    if shots:
        write_shots(con, season, shots)
    print("  %s: parsed %d games, %d player lines, %d shots%s" % (
        season, len(games), lines_n, len(shots),
        (", %d people not yet in the registry (run: players)" % len(unknown)) if unknown else ""))
    return [g[0] for g in games]


def write_shots(con, season, rows):
    """Add a season's newly parsed shots to its shot file (replacing those games)."""
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, "%s.parquet" % season)
    types = ["VARCHAR", "VARCHAR", "VARCHAR", "DATE", "INTEGER", "INTEGER", "VARCHAR", "VARCHAR",
             "VARCHAR", "DOUBLE", "DOUBLE", "DOUBLE", "BOOLEAN", "INTEGER", "VARCHAR", "VARCHAR", "VARCHAR"]
    con.execute("CREATE OR REPLACE TEMP TABLE gl_new_shots (%s)" %
                ", ".join("%s %s" % (c, t) for c, t in zip(SHOT_COLS, types)))
    con.executemany("INSERT INTO gl_new_shots VALUES (%s)" % ",".join("?" * len(SHOT_COLS)), rows)
    if os.path.exists(path):
        con.execute("""CREATE OR REPLACE TEMP TABLE gl_all_shots AS
                       SELECT * FROM read_parquet('%s') WHERE game_id NOT IN (SELECT game_id FROM gl_new_shots)
                       UNION ALL SELECT * FROM gl_new_shots""" % path)
    else:
        con.execute("CREATE OR REPLACE TEMP TABLE gl_all_shots AS SELECT * FROM gl_new_shots")
    tmp = path + ".tmp"
    con.execute("COPY (SELECT * FROM gl_all_shots ORDER BY game_date, game_id, action_number) "
                "TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % tmp)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

COUNTED = "season_type NOT IN ('preseason', 'allstar')"

PLAYER_SEASON = """
    SELECT player_id, ANY_VALUE(name) AS name, season,
           string_agg(DISTINCT team_abbr, '/') AS teams,
           arg_max(team_abbr, game_date) AS last_team,
           COUNT(*) FILTER (WHERE played) AS gp, COUNT(*) FILTER (WHERE started) AS gs,
           ROUND(SUM(seconds_played) / 60.0, 1) AS mp,
           SUM(fgm) fgm, SUM(fga) fga, SUM(fg3m) fg3m, SUM(fg3a) fg3a, SUM(ftm) ftm, SUM(fta) fta,
           SUM(orb) orb, SUM(drb) drb, SUM(trb) trb, SUM(ast) ast, SUM(stl) stl, SUM(blk) blk,
           SUM(tov) tov, SUM(pf) pf, SUM(pts) pts, SUM(plus_minus) plus_minus
    FROM gl_player_game
    WHERE season = '{s}' AND {counted} AND player_id IS NOT NULL
    GROUP BY player_id, season
    HAVING COUNT(*) FILTER (WHERE played) > 0"""

TEAM_SEASON = """
    WITH t AS (
        SELECT team_id, team_abbr, game_id, ANY_VALUE(is_home) is_home,
               SUM(pts) pts, SUM(fgm) fgm, SUM(fga) fga, SUM(fg3m) fg3m, SUM(fg3a) fg3a,
               SUM(ftm) ftm, SUM(fta) fta, SUM(orb) orb, SUM(drb) drb, SUM(ast) ast,
               SUM(stl) stl, SUM(blk) blk, SUM(tov) tov, SUM(pf) pf
        FROM gl_player_game WHERE season = '{s}' AND {counted}
        GROUP BY team_id, team_abbr, game_id)
    SELECT a.team_id, arg_max(a.team_abbr, a.game_id) team_abbr, COUNT(*) gp,
           SUM((a.pts > b.pts)::INT) w, SUM((a.pts < b.pts)::INT) l,
           SUM(a.pts) pts, SUM(a.fgm) fgm, SUM(a.fga) fga, SUM(a.fg3m) fg3m, SUM(a.fg3a) fg3a,
           SUM(a.ftm) ftm, SUM(a.fta) fta, SUM(a.orb) orb, SUM(a.drb) drb, SUM(a.ast) ast,
           SUM(a.stl) stl, SUM(a.blk) blk, SUM(a.tov) tov, SUM(a.pf) pf,
           SUM(b.pts) opp_pts, SUM(b.fgm) opp_fgm, SUM(b.fga) opp_fga, SUM(b.fg3m) opp_fg3m,
           SUM(b.fg3a) opp_fg3a, SUM(b.ftm) opp_ftm, SUM(b.fta) opp_fta, SUM(b.orb) opp_orb,
           SUM(b.drb) opp_drb, SUM(b.ast) opp_ast, SUM(b.stl) opp_stl, SUM(b.blk) opp_blk,
           SUM(b.tov) opp_tov, SUM(b.pf) opp_pf,
           ROUND(SUM(a.fga - a.orb + a.tov + 0.44 * a.fta), 1) poss_est
    FROM t a JOIN t b ON a.game_id = b.game_id AND a.team_id <> b.team_id
    GROUP BY a.team_id"""


# a player's game lines with the game's final score and label
WITH_SCORES = """
    SELECT p.* EXCLUDE (person_id), g.home_score, g.away_score, g.label
    FROM gl_player_game p JOIN gl_games g USING (game_id)
    WHERE %s ORDER BY p.game_date, p.game_id"""


def cmd_export(con, season, s3, bucket, players=None):
    """Season files, then each affected player's career G League game log."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def put(sql, key):
            p = os.path.join(tmp, key.replace("/", "_"))
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (sql, p))
            s3.upload_file(p, bucket, "%s/gleague/%s" % (PREFIX, key))
        base = "seasons/%s/" % season
        put(WITH_SCORES % ("p.season = '%s'" % season), base + "player_game.parquet")
        put(PLAYER_SEASON.format(s=season, counted=COUNTED), base + "player_season.parquet")
        put(TEAM_SEASON.format(s=season, counted=COUNTED), base + "team_season.parquet")
        put("SELECT * EXCLUDE (fetched, parsed, error, updated_at) FROM gl_games WHERE season = '%s' "
            "ORDER BY game_date, game_id" % season, base + "games.parquet")
        put("SELECT * FROM gl_teams ORDER BY season, abbr", "teams.parquet")
        shots = os.path.join(SHOTS, "%s.parquet" % season)
        if os.path.exists(shots):
            s3.upload_file(shots, bucket, "%s/gleague/shots/%s.parquet" % (PREFIX, season))

    if players is None:
        players = [r[0] for r in con.execute(
            "SELECT DISTINCT player_id FROM gl_player_game WHERE season = ? AND player_id IS NOT NULL",
            [season]).fetchall()]
    export_players(con, players, s3, bucket)
    print("  %s: exported season files and %d player game logs" % (season, len(players)))


def export_players(con, players, s3, bucket):
    """Each player's whole G League career, one file."""
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    with tempfile.TemporaryDirectory() as tmp:
        jobs = []
        for pid in players:
            p = os.path.join(tmp, "%s.parquet" % pid)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (WITH_SCORES % ("p.player_id = '%s'" % pid), p))
            jobs.append((p, "%s/gleague/players/%s/gamelog.parquet" % (PREFIX, pid)))
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(lambda j: s3.upload_file(j[0], bucket, j[1]), jobs))


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def cmd_check(con, season):
    rows = con.execute("""
        SELECT g.game_id, g.home_abbr, g.away_abbr, g.home_score, g.away_score,
               SUM(p.pts) FILTER (WHERE p.is_home) hp, SUM(p.pts) FILTER (WHERE NOT p.is_home) ap,
               COUNT(*) FILTER (WHERE p.player_id IS NULL) unlinked, g.shots
        FROM gl_games g JOIN gl_player_game p USING (game_id)
        WHERE g.season = ? GROUP BY ALL ORDER BY g.game_id""", [season]).fetchall()
    bad = [r for r in rows if r[3] is not None and (r[3] != r[5] or r[4] != r[6])]
    print("  %s: %d games parsed; box totals match the final score in %d" % (season, len(rows), len(rows) - len(bad)))
    for r in bad[:10]:
        print("    %s %s %s-%s %s: box says %s-%s" % (r[0], r[2], r[4], r[3], r[1], r[6], r[5]))
    print("  lines without a registry id: %d; games with shots: %d of %d" % (
        sum(r[7] for r in rows), sum(1 for r in rows if r[8]), len(rows)))


def cmd_sample(gid, relay=False):
    for ep, params in (("boxscoretraditionalv3", {"GameID": gid, "StartPeriod": 0, "EndPeriod": 0,
                                                 "StartRange": 0, "EndRange": 0, "RangeType": 0}),
                       ("playbyplayv3", {"GameID": gid, "StartPeriod": 0, "EndPeriod": 0})):
        j = stats_get(ep, params, relay)
        if not j:
            print("  %s: nothing" % ep)
            continue
        if ep.startswith("box"):
            b = j.get("boxScoreTraditional") or {}
            t = b.get("homeTeam") or {}
            p = (t.get("players") or [{}])[0]
            print("  box keys:", list(b.keys())[:12])
            print("  team keys:", list(t.keys()))
            print("  player:", {k: v for k, v in p.items() if k != "statistics"})
            print("  statistics:", p.get("statistics"))
        else:
            a = [x for x in (j.get("game") or {}).get("actions") or [] if x.get("isFieldGoal") == 1][:1]
            print("  first shot:", a[0] if a else None)


# ---------------------------------------------------------------------------

def run_season(season, limit=None, relay=False, export=True):
    con = db()
    try:
        final = cmd_schedule(con, season, relay)
        con.commit()
        if final:
            cmd_fetch(con, season, limit, relay)
            cmd_parse(con, season)
        if export:
            s3, bucket = s3_client()
            cmd_export(con, season, s3, bucket)
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["players", "run", "backfill", "download", "check", "sample", "export",
                                    "repair"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--game")
    ap.add_argument("--relay", action="store_true", help="go through the Worker even on the Mac")
    ap.add_argument("--current", action="store_true", help="players: this season's only")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--sync", action="store_true",
                    help="on the Mac: lock, pull the database from R2, write, push it back")
    a = ap.parse_args()
    if a.relay or os.environ.get("GITHUB_ACTIONS"):
        import nightly2 as N
        for k in ("NBA_RELAY_URL", "NBA_RELAY_TOKEN"):
            if not os.environ.get(k) and N.load_env().get(k):
                os.environ[k] = N.load_env()[k]

    # On the Mac, the working database's master copy is in R2: take the lock,
    # pull it, write, push it back. The nightly does this itself, so it calls
    # without --sync.
    writes = a.cmd in ("players", "run", "backfill", "repair")
    if a.sync and writes:
        import nightly2 as N
        env = N.load_env()
        s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
        log = N.Log()
        if not N.acquire_lock(s3, bucket, log):
            sys.exit("  another run holds the lock — try again when it finishes")
        try:
            if not N.pull_db(s3, bucket, log):
                sys.exit("  could not pull the database")
            a.sync = False
            export_after = a.cmd in ("backfill", "repair") and not a.no_export
            if export_after:
                a.no_export = True          # exports need no lock: after it is released
            dispatch(a)
            N.push_db(s3, bucket, log)
        finally:
            N.release_lock(s3, bucket)
        if export_after:
            print("\n  lock released — writing the season files and career game logs")
            export_seasons(season_list(a.seasons) if a.seasons else all_seasons())
        return
    dispatch(a)


def dispatch(a):
    if a.cmd == "repair":
        cmd_repair()
        if not a.no_export:
            export_seasons(all_seasons())
    elif a.cmd == "players":
        cmd_players(a.current, a.relay)
    elif a.cmd == "sample":
        cmd_sample(a.game, a.relay)
    elif a.cmd == "check":
        con = db()
        cmd_check(con, a.season)
        con.close()
    elif a.cmd == "export":
        con = db()
        s3, bucket = s3_client()
        cmd_export(con, a.season, s3, bucket)
        con.close()
    elif a.cmd == "download":
        cmd_download(season_list(a.seasons) if a.seasons else [a.season], a.relay)
    elif a.cmd == "run":
        run_season(a.season, a.limit, a.relay, not a.no_export)
    elif a.cmd == "backfill":
        # fetch and parse every season first; then each season's files, and
        # each player's career file once at the end rather than once a season
        seasons = season_list(a.seasons)
        for s in seasons:
            print("\n== %s" % s)
            run_season(s, a.limit, a.relay, export=False)
        if not a.no_export:
            export_seasons(seasons)


def cmd_repair():
    """Name the game types we had not, and register the people with box lines
    but no player record."""
    con = db()
    try:
        for code, t in (("203", "allstar"), ("207", "showcase")):
            n = con.execute("SELECT COUNT(*) FROM gl_games WHERE substr(game_id, 1, 3) = ? "
                            "AND season_type <> ?", [code, t]).fetchone()[0]
            con.execute("UPDATE gl_games SET season_type = ? WHERE substr(game_id, 1, 3) = ?", [t, code])
            con.execute("UPDATE gl_player_game SET season_type = ? WHERE substr(game_id, 1, 3) = ?", [t, code])
            print("  %s games refiled as %s: %d" % (code, t, n))
        con.commit()
        before = con.execute("SELECT COUNT(DISTINCT person_id) FROM gl_player_game WHERE player_id IS NULL").fetchone()[0]
    finally:
        con.close()
    print("  people with box lines but no player record: %d" % before)
    cmd_players(from_boxes=True)
    con = db()
    after = con.execute("SELECT COUNT(DISTINCT person_id) FROM gl_player_game WHERE player_id IS NULL").fetchone()[0]
    con.close()
    print("  still without a record (staged for review): %d" % after)


def all_seasons():
    con = db()
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT season FROM gl_games ORDER BY season").fetchall()]
    finally:
        con.close()


def export_seasons(seasons):
    """Each season's files, then every G League player's career game log once."""
    con = db()
    try:
        s3, bucket = s3_client()
        for s in seasons:
            cmd_export(con, s, s3, bucket, players=[])
        everyone = [r[0] for r in con.execute(
            "SELECT DISTINCT player_id FROM gl_player_game WHERE player_id IS NOT NULL").fetchall()]
        export_players(con, everyone, s3, bucket)
        print("  career game logs: %d players" % len(everyone))
    finally:
        con.close()


if __name__ == "__main__":
    main()
