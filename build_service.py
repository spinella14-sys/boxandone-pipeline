#!/usr/bin/env python3
"""
build_service.py — Years of Service, two-way seasons, and whether each player
can sign a two-way contract.

THE RULES (2023 CBA, as Hoops Rumors' glossary states them)
  - a player can sign a two-way with fewer than four Years of Service
  - a two-way runs one or two seasons, no options; with three years he is
    limited to one season
  - no more than three seasons on two-ways with the same team
  - the "Harry Giles" exception: exactly four years, one of them a whole
    regular season on a roster without appearing in a game -> one-year deal
  - a Year of Service is credited at the end of the league year (June 30) for
    a season with at least one day on an Active or Inactive List

ONE CHOICE, MADE ON ADVICE
  A season spent only on a two-way does not earn a Year of Service — Adam's
  league contact. Hoops Rumors writes as though it does. TWO_WAY_EARNS_YOS
  holds the choice; every result says which rule produced it.

HOW A SEASON IS COUNTED
  A season earns a year when the player
    (a) appeared in a regular-season game while not on a two-way with that
        team, or
    (b) signed a standard contract in that league year that ran into the
        regular season (from July 2015, when NBA.com's feed begins) — this
        catches a season on the roster without playing.
  It is a two-way season with a team when a two-way with that team overlaps
  the regular season and there's evidence he was on it that season: he
  signed it that league year, appeared for the team on it, or is on the
  league's two-way list for the team now.

  Contract history: NBA.com's transaction feed (July 2015 on), filled in with
  Basketball Reference's two-way signings and conversions, and the current
  two-way list. Appearances: every season's game files back to 1996-97.

  What can't be seen: a season on a roster without playing before July 2015,
  or under a multi-year contract signed in an earlier league year. Players
  whose careers reach back before 2015-16, or with a gap between seasons
  that did count, are marked medium confidence.

    python3 build_service.py history          # once, on the Mac: every past season's appearances
    python3 build_service.py build            # nightly: everyone's eligibility -> v2/eligibility/players.parquet
    python3 build_service.py explain "RayJ Dennis"
"""
import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import nightly2 as N  # noqa: E402

PREFIX = "v2"
TWO_WAY_EARNS_YOS = False
FEED_FROM = date(2015, 7, 1)
SAME_TEAM_MAX = 3
LOCAL = os.path.join(HOME, "data", "service")
# Basketball Reference and older codes -> the NBA.com code the feed uses
CANON = {"BRK": "BKN", "NJN": "BKN", "PHO": "PHX", "CHO": "CHA", "CHH": "CHA", "NOH": "NOP", "NOK": "NOP",
         "SEA": "OKC", "VAN": "MEM", "WSB": "WAS", "GOS": "GSW", "SAN": "SAS", "UTH": "UTA", "NYN": "NYK"}
ORDER = {"waive": 0, "trade": 1, "claim": 2, "sign": 3, "twoway_sign": 3, "convert_to_twoway": 4,
         "convert_to_standard": 4}


def canon(t):
    return CANON.get(t, t) if t else t


def season_str(y):
    return "%d-%02d" % (y, (y + 1) % 100)


def league_year(d):
    """July 1 starts the league year (and so the season) it belongs to."""
    return season_str(d.year if d.month >= 7 else d.year - 1)


def as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# appearances
# ---------------------------------------------------------------------------

def cmd_history(s3, bucket):
    """Every completed season's regular-season appearances and season dates,
    from the season files in R2, into one small file (run once; history
    doesn't change)."""
    import duckdb
    cur = N.current_season()
    first = 1996
    last = int(cur[:4]) - 1
    con = duckdb.connect()
    os.makedirs(LOCAL, exist_ok=True)
    apps, bounds = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for y in range(first, last + 1):
            s = season_str(y)
            p = os.path.join(tmp, "%s.parquet" % s)
            try:
                s3.download_file(bucket, "%s/seasons/%s/player_game.parquet" % (PREFIX, s), p)
            except Exception as e:
                print("  %s: not found (%s)" % (s, str(e)[:40]))
                continue
            apps += con.execute("""
                SELECT player_id, '%s' season, team_abbr, list(CAST(game_date AS DATE) ORDER BY game_date)
                FROM read_parquet('%s') WHERE season_type = 'regular' AND player_id IS NOT NULL
                GROUP BY ALL""" % (s, p)).fetchall()
            b = con.execute("""SELECT MIN(CAST(game_date AS DATE)), MAX(CAST(game_date AS DATE))
                               FROM read_parquet('%s') WHERE season_type = 'regular'""" % p).fetchone()
            bounds.append((s, b[0], b[1]))
            print("  %s: %d player-team seasons" % (s, len(apps)), end="\r")
    print()
    con.execute("CREATE TABLE a (player_id VARCHAR, season VARCHAR, team_abbr VARCHAR, dates DATE[])")
    con.executemany("INSERT INTO a VALUES (?,?,?,?)", apps)
    con.execute("CREATE TABLE b (season VARCHAR, start_date DATE, end_date DATE)")
    con.executemany("INSERT INTO b VALUES (?,?,?)", bounds)
    for t, name in (("a", "history"), ("b", "seasons")):
        p = os.path.join(LOCAL, "%s.parquet" % name)
        con.execute("COPY %s TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (t, p))
        s3.upload_file(p, bucket, "%s/service/%s.parquet" % (PREFIX, name))
    print("  history: %d player-team seasons over %d seasons -> v2/service/" % (len(apps), len(bounds)))


def load_history(s3, bucket):
    import duckdb
    os.makedirs(LOCAL, exist_ok=True)
    for name in ("history", "seasons"):
        p = os.path.join(LOCAL, "%s.parquet" % name)
        try:
            s3.download_file(bucket, "%s/service/%s.parquet" % (PREFIX, name), p)
        except Exception:
            if not os.path.exists(p):
                sys.exit("  no v2/service/%s.parquet — run `python3 build_service.py history` once" % name)
    c = duckdb.connect()
    apps = c.execute("SELECT * FROM read_parquet('%s')" % os.path.join(LOCAL, "history.parquet")).fetchall()
    bounds = {s: (a, b) for s, a, b in c.execute(
        "SELECT * FROM read_parquet('%s')" % os.path.join(LOCAL, "seasons.parquet")).fetchall()}
    return apps, bounds


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------

def load_events(con, season, bounds):
    """player_id -> [(date, kind, team, source, text)], oldest first."""
    ev = defaultdict(list)
    for pid, d, kind, team, desc in con.execute("""
            SELECT player_id, txn_date, kind, team_abbr, description FROM nba_movement
            WHERE player_id IS NOT NULL""").fetchall():
        if kind in ORDER:
            ev[pid].append((as_date(d), kind, canon(team), "NBA.com", desc))
    # Basketball Reference two-way signings and conversions the feed lacks
    try:
        rows = con.execute("""
            SELECT i.player_id, t.txn_date, t.raw_text, i.to_team, i.from_team
            FROM transactions t JOIN transaction_items i USING (transaction_id)
            WHERE t.txn_type IN ('two_way', 'convert') AND i.player_id IS NOT NULL""").fetchall()
    except Exception:
        rows = []
    for pid, d, text, to_t, from_t in rows:
        low = (text or "").lower()
        if "convert" in low:
            kind = "convert_to_twoway" if re.search(r"to a two-way", low) else "convert_to_standard"
        elif "two-way" in low and "sign" in low:
            kind = "twoway_sign"
        else:
            continue
        d = as_date(d)
        if any(k == kind and abs((x - d).days) <= 3 for x, k, *_ in ev[pid]):
            continue
        ev[pid].append((d, kind, canon(to_t or from_t), "Basketball Reference", text))
    # on the league's two-way list right now
    start = bounds.get(season, (None, None))[0]
    try:
        for pid, team in con.execute("""
                SELECT player_id, nba_abbr FROM twoway_list
                WHERE snap_date = (SELECT MAX(snap_date) FROM twoway_list) AND player_id IS NOT NULL""").fetchall():
            ev[pid].append(("LIST", team))
    except Exception:
        pass
    for pid in ev:
        listed = [e for e in ev[pid] if e[0] == "LIST"]
        ev[pid] = sorted((e for e in ev[pid] if e[0] != "LIST"), key=lambda e: (e[0], ORDER.get(e[1], 9)))
        for _, team in listed:
            ev[pid].append(("LIST", team))
    return ev


def intervals(events, today):
    """Contract spells from the events: (type, team, start, end or None, source)."""
    out, cur, last = [], None, None
    def close(d):
        nonlocal cur, last
        if cur:
            cur["end"] = d
            out.append(cur)
            last = cur
            cur = None
    for e in events:
        if e[0] == "LIST":
            continue
        d, kind, team, src, _ = e
        if kind == "waive":
            close(d)
        elif kind == "trade":
            t = cur["type"] if cur else "standard"
            close(d)
            cur = {"type": t, "team": team, "start": d, "end": None, "source": src}
        elif kind == "claim":
            t = last["type"] if last and last["end"] and (d - last["end"]).days <= 7 else "standard"
            close(d)
            cur = {"type": t, "team": team, "start": d, "end": None, "source": src}
        elif kind in ("sign", "convert_to_standard"):
            close(d)
            cur = {"type": "standard", "team": team or (last or {}).get("team"), "start": d, "end": None, "source": src}
        elif kind in ("twoway_sign", "convert_to_twoway"):
            prev = cur
            close(d)
            cur = {"type": "two_way", "team": team or (prev or last or {}).get("team"), "start": d, "end": None,
                   "source": src}
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# a player's seasons and eligibility
# ---------------------------------------------------------------------------

def assess(pid, apps, events, bounds, season, today):
    """apps: {(season, team): [dates]}."""
    ivs = intervals(events, today)
    listed = {e[1] for e in events if e[0] == "LIST"}
    for team in listed:     # on the list now with no recorded two-way: add one for this season
        if not any(i["type"] == "two_way" and i["team"] == team and i["end"] is None for i in ivs):
            ivs.append({"type": "two_way", "team": team, "start": bounds.get(season, (today,))[0] or today,
                        "end": None, "source": "two-way list"})
    seasons = sorted({s for s, _ in apps} | {league_year(i["start"]) for i in ivs if i["start"] >= FEED_FROM})
    rows = []
    for s in seasons:
        if s not in bounds:
            continue
        s0, s1 = bounds[s]
        tw = [i for i in ivs if i["type"] == "two_way" and i["start"] <= s1 and (i["end"] is None or i["end"] >= s0)]
        std_games = tw_games = 0
        tw_teams = set()
        for (ss, team), dates in apps.items():
            if ss != s:
                continue
            for d in dates:
                d = as_date(d)
                on_tw = any(i["team"] == canon(team) and i["start"] <= d and (i["end"] is None or i["end"] >= d)
                            for i in tw)
                if on_tw:
                    tw_games += 1
                    tw_teams.add(canon(team))
                else:
                    std_games += 1
        for i in tw:        # a two-way season needs evidence it was this season's
            if league_year(i["start"]) == s or (s == season and i["team"] in listed):
                tw_teams.add(i["team"])
        contract = [i for i in ivs if i["type"] == "standard" and league_year(i["start"]) == s
                    and i["start"] <= s1 and (i["end"] is None or i["end"] >= s0)]
        how = "games" if std_games else "contract" if contract else None
        if TWO_WAY_EARNS_YOS and not how and (tw_games or tw_teams):
            how = "two-way"
        rows.append({"season": s, "yos": bool(how), "how": how, "std_games": std_games, "tw_games": tw_games,
                     "tw_teams": sorted(t for t in tw_teams if t)})

    done = [r for r in rows if r["season"] < season]
    yos = sum(r["yos"] for r in done)
    now = next((r for r in rows if r["season"] == season), None)
    yos_next = yos + (1 if now and now["yos"] else 0)
    tw_by_team = defaultdict(int)
    for r in rows:
        for t in r["tw_teams"]:
            tw_by_team[t] += 1
    blocked = sorted(t for t, n in tw_by_team.items() if n >= SAME_TEAM_MAX)
    giles = yos == 4 and any(r["yos"] and r["how"] == "contract" and not r["std_games"] and not r["tw_games"]
                             for r in done)
    if yos <= 2:
        status, years = "eligible", 2
    elif yos == 3:
        status, years = "one_year", 1
    elif giles:
        status, years = "exception", 1
    else:
        status, years = "ineligible", 0

    notes, conf = [], "high"
    counted = [r["season"] for r in done if r["yos"]]
    if counted and counted[0] < "2015-16" and yos <= 5:
        conf = "medium"
        notes.append("Career reaches back before July 2015, when contract records begin: a season on a "
                     "roster without playing before then can't be seen.")
    if counted:
        allss = [season_str(y) for y in range(int(counted[0][:4]), int(counted[-1][:4]) + 1)]
        missing = [s for s in allss if s not in counted]
        if missing and yos <= 5:
            conf = "medium"
            notes.append("No year counted in %s, between seasons that did: a full season on a roster under an "
                         "earlier multi-year contract would still count." % ", ".join(missing))
    if giles:
        notes.append("Possible Harry Giles exception: a counted season with no games. It also needs no playoff "
                     "games and a whole regular season on the roster — check before relying on it.")
    if not TWO_WAY_EARNS_YOS and any(r["tw_teams"] and not r["yos"] for r in done):
        notes.append("Two-way-only seasons are not counted as Years of Service (on league advice; Hoops Rumors' "
                     "glossary reads the other way).")
    return {
        "yos": yos, "yos_next": yos_next, "status": status, "max_years": years,
        "tw_seasons": sum(1 for r in rows if r["tw_teams"]), "tw_by_team": dict(tw_by_team),
        "blocked": blocked, "giles": giles, "confidence": conf, "notes": notes,
        "on_twoway": sorted(listed)[0] if listed else None, "seasons": rows,
        "contracts": [{"type": i["type"], "team": i["team"], "start": str(i["start"]),
                       "end": str(i["end"]) if i["end"] else None, "source": i["source"]} for i in ivs],
    }


def assess_all(con, s3, bucket):
    season = N.current_season()
    today = datetime.now().date()
    hist, bounds = load_history(s3, bucket)
    b = con.execute("""SELECT MIN(game_date), MAX(game_date) FROM scheduled_games
                       WHERE season = ? AND season_type = 'regular'""", [season]).fetchone()
    if b[0]:
        bounds[season] = (as_date(b[0]), as_date(b[1]))
    apps = defaultdict(lambda: defaultdict(list))
    for pid, s, team, dates in hist:
        apps[pid][(s, team)] = dates
    for pid, team, dates in con.execute("""
            SELECT b.player_id, b.team_abbr, list(g.game_date ORDER BY g.game_date)
            FROM player_game_box b JOIN games g USING (game_id)
            WHERE g.season = ? AND g.season_type = 'regular' AND b.played AND b.player_id IS NOT NULL
            GROUP BY ALL""", [season]).fetchall():
        apps[pid][(season, team)] = dates
    events = load_events(con, season, bounds)
    people = set(apps) | set(events)
    try:
        people |= {r[0] for r in con.execute(
            "SELECT DISTINCT player_id FROM gl_player_game WHERE player_id IS NOT NULL").fetchall()}
    except Exception:
        pass
    names = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())
    out = {}
    for pid in people:
        if pid not in names:
            continue
        out[pid] = assess(pid, apps.get(pid, {}), events.get(pid, []), bounds, season, today)
        out[pid]["name"] = names[pid]
    return out, season


def cmd_build(con, s3, bucket):
    import duckdb
    res, season = assess_all(con, s3, bucket)
    c = duckdb.connect()
    c.execute("""CREATE TABLE e (player_id VARCHAR, name VARCHAR, season VARCHAR, yos INTEGER, yos_next INTEGER,
                 status VARCHAR, max_years INTEGER, tw_seasons INTEGER, blocked VARCHAR, giles BOOLEAN,
                 confidence VARCHAR, on_twoway VARCHAR, detail VARCHAR, built_at TIMESTAMP)""")
    now = datetime.utcnow()
    c.executemany("INSERT INTO e VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (pid, r["name"], season, r["yos"], r["yos_next"], r["status"], r["max_years"], r["tw_seasons"],
         ",".join(r["blocked"]), r["giles"], r["confidence"], r["on_twoway"],
         json.dumps({k: r[k] for k in ("tw_by_team", "notes", "seasons", "contracts")}, default=str), now)
        for pid, r in res.items()])
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "players.parquet")
        c.execute("COPY (SELECT * FROM e ORDER BY player_id) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % p)
        s3.upload_file(p, bucket, "%s/eligibility/players.parquet" % PREFIX)
    by = defaultdict(int)
    for r in res.values():
        by[r["status"]] += 1
    print("  eligibility: %d players — %s; medium confidence %d" % (
        len(res), ", ".join("%s %d" % kv for kv in sorted(by.items())),
        sum(1 for r in res.values() if r["confidence"] != "high")))


def cmd_explain(con, s3, bucket, name):
    import ingest_rosters as R
    res, season = assess_all(con, s3, bucket)
    hits = [(pid, r) for pid, r in res.items() if R.norm(r["name"]) == R.norm(name)]
    if not hits:
        print("  no player named %r" % name)
    for pid, r in hits:
        print("\n  %s (%s) — %s: %d Years of Service (%d after this season); %s%s; confidence %s" % (
            r["name"], pid, season, r["yos"], r["yos_next"], r["status"],
            " for up to %d season%s" % (r["max_years"], "s" if r["max_years"] > 1 else "") if r["max_years"] else "",
            r["confidence"]))
        print("  two-way seasons by team: %s%s" % (r["tw_by_team"] or "none",
              "; can't sign another with %s" % ", ".join(r["blocked"]) if r["blocked"] else ""))
        print("  contracts:")
        for c in r["contracts"]:
            print("    %-9s %-4s %s -> %s  (%s)" % (c["type"], c["team"], c["start"], c["end"] or "open", c["source"]))
        print("  seasons:")
        for s in r["seasons"]:
            print("    %s  %-5s %-9s standard games %3d  two-way games %3d  two-way with %s" % (
                s["season"], "YES" if s["yos"] else "no", s["how"] or "", s["std_games"], s["tw_games"],
                ", ".join(s["tw_teams"]) or "-"))
        for n in r["notes"]:
            print("  note: " + n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["history", "build", "explain"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--sync", action="store_true", help="on the Mac: pull the latest database first (read only)")
    a = ap.parse_args()
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    if a.cmd == "history":
        cmd_history(s3, bucket)
        return
    if a.sync:
        log = N.Log()
        if not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    try:
        if a.cmd == "build":
            cmd_build(con, s3, bucket)
        else:
            cmd_explain(con, s3, bucket, a.name or "")
    finally:
        con.close()


if __name__ == "__main__":
    main()
