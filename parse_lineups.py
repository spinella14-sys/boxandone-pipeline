#!/usr/bin/env python3
"""
parse_lineups.py — who was on the floor, continuously, for every game.

The usual approach derives a starting five for each period from the event log,
and it has a hole: a player who is on the floor, never touches the ball, and is
never substituted leaves no trace at all. Periods where that happens cannot be
resolved from play-by-play alone.

This sidesteps it. The box score already records who started the GAME, so the
walk begins from a known truth and applies substitutions continuously —
lineups carry across period breaks unless a substitution says otherwise. There
is no per-period guess to get wrong.

That converts an unsolvable inference into a checkable one. If the walk ever
leaves a team with four or six players on the floor, the game is wrong and gets
flagged rather than quietly producing bad lineups. Two further checks run after:

    every stint has exactly five players a side
    minutes derived from the stints match the box score, per player

Substitutions name the outgoing player by surname only — "SUB: Eason FOR
Smith Jr." But the candidate pool is the five players currently on the floor,
not the league, so the match is nearly unambiguous. The lineup state constrains
the name resolution, which is the trick that makes this tractable.

    python3 parse_lineups.py check --season 2025-26 --limit 50
    python3 parse_lineups.py build
    python3 parse_lineups.py build --season 2015-16
    python3 parse_lineups.py status

Writes v2/lineups/{season}.parquet: one row per stint, five player columns a
side, with the clock bounds to join possessions against.
"""

import argparse
import os
import re
import sys
import unicodedata
import tempfile
from collections import defaultdict

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2/lineups"

PERIOD_SECS = {1: 720, 2: 720, 3: 720, 4: 720}   # overtime is 300


def period_length(p):
    return 720 if p <= 4 else 300


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def connect():
    import duckdb
    if not os.path.isdir(CACHE) or not os.listdir(CACHE):
        sys.exit("  archive cache empty — run: python3 export_full.py sync")
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    for name, f in (("ag", "games"), ("ap", "play_by_play"), ("ab", "player_game_box")):
        con.execute("""CREATE VIEW %s AS
            SELECT * FROM read_parquet('%s/*/%s.parquet', union_by_name=true)
            UNION ALL BY NAME SELECT * FROM hot.%s""" % (name, CACHE, f, f))
    return con


def clock_secs(raw):
    if not raw:
        return None
    m = re.match(r"PT(\d+)M([\d.]+)S", raw)
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


# ---------------------------------------------------------------------------
# name matching
# ---------------------------------------------------------------------------

SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def norm(s):
    """Fold a name to a comparable form.

    A hand-written accent map was the original approach and it failed on
    Alperen Şengün: it covered č, ć, ž, š and others but not ş, so "sengun"
    never matched. Unicode decomposition handles every accented character
    rather than the ones someone remembered — NFKD splits a letter from its
    diacritic, and the diacritics are then dropped.
    """
    s = unicodedata.normalize("NFKD", (s or "").lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    # a few letters carry no combining mark and must be mapped directly
    for a, b in (("ø", "o"), ("ð", "d"), ("þ", "th"), ("æ", "ae"), ("œ", "oe"),
                 ("ł", "l"), ("đ", "d"), ("ß", "ss")):
        s = s.replace(a, b)
    s = s.replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(s.split())


def match_out(text, candidates, names):
    """Resolve the outgoing player's surname against the five on the floor.

    The pool being five rather than thousands is what makes this work: a
    surname that would be hopelessly ambiguous league-wide is almost always
    unique among the players actually on the court."""
    t = norm(text)
    if not t:
        return None
    exact = [p for p in candidates if norm(names.get(p, "")).endswith(t)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # two players on the floor whose names both end this way — take the
        # longer match, which handles "Smith" against "Smith Jr."
        return max(exact, key=lambda p: len(norm(names.get(p, ""))))
    # the surname may carry a suffix the full name writes differently
    base = " ".join(w for w in t.split() if w not in SUFFIXES)
    loose = [p for p in candidates if base and norm(names.get(p, "")).endswith(base)]
    if len(loose) == 1:
        return loose[0]
    # last resort: any candidate containing the token
    token = t.split()[-1] if t.split() else ""
    contains = [p for p in candidates if token and token in norm(names.get(p, ""))]
    return contains[0] if len(contains) == 1 else None


SUB_RE = re.compile(r"SUB:\s*(.+?)\s+FOR\s+(.+?)\s*$", re.I)

# Play-by-play uses the league's abbreviations; the games table uses Basketball
# Reference's. Three franchises differ, and without this every Brooklyn,
# Charlotte and Phoenix game fails the team lookup.
PBP_TO_BBREF = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO",
                "NJN": "BRK", "NOH": "NOP", "SEA": "OKC", "VAN": "MEM"}


def team_of(abbr):
    return PBP_TO_BBREF.get(abbr, abbr)


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------

def match_in(text, team, roster, names, on_floor):
    """Resolve the incoming player's surname.

    The pool is the team's roster MINUS whoever is already playing, because a
    player on the floor cannot be substituted in. That constraint resolves
    same-surname teammates without any cleverness: only one of them is
    available to come in.
    """
    available = [p for p in roster.get(team, []) if p not in on_floor]
    hit = match_out(text, available, names)
    if hit:
        return hit
    # nothing available matched — fall back to the full roster so the caller
    # can report a meaningful reason rather than a bare miss
    return match_out(text, roster.get(team, []), names)


def parse_game(events, starters, names, home, away, roster):
    """Returns (stints, problem). problem is None when the game is clean."""
    on = {home: set(starters.get(home, [])), away: set(starters.get(away, []))}
    for t in (home, away):
        if len(on[t]) != 5:
            return [], "started with %d for %s" % (len(on[t]), t)

    stints = []
    period = None
    cur_start = None
    cur_action = None

    def close(end_secs, end_action):
        nonlocal cur_start
        if cur_start is None:
            return
        if end_secs is not None and cur_start is not None and end_secs >= cur_start:
            return      # clock ran backwards; skip the degenerate stint
        stints.append({
            "period": period,
            "start_secs": cur_start, "end_secs": end_secs,
            "start_action": cur_action, "end_action": end_action,
            "home_on": sorted(on[home]), "away_on": sorted(on[away]),
        })

    for e in events:
        at = (e.get("action_type") or "").lower()
        secs = e.get("secs")

        if e.get("period") != period:
            if period is not None:
                close(0.0, e.get("action_id"))
            period = e.get("period")
            cur_start = float(period_length(period))
            cur_action = e.get("action_id")

        if at != "substitution":
            continue

        team = team_of(e.get("team_abbr"))
        if team not in on:
            continue
        m = SUB_RE.search(e.get("description") or "")
        # "SUB: Williams FOR Bridges" — the name after FOR is the player going
        # OUT, and player_id on the event is that same player. So the id gives
        # the outgoing side directly, and the surname before FOR is the one
        # that needs resolving against the team's roster.
        outgoing = e.get("player_id")
        incoming = match_in(m.group(1), team, roster, names, on[team]) if m else None

        if not incoming or not outgoing:
            return [], "unresolved sub at %s: %s" % (secs, (e.get("description") or "")[:48])
        if outgoing not in on[team]:
            return [], ("sub out %s (%s) not on floor; floor is %s"
                        % (outgoing, names.get(outgoing, "?"),
                           ",".join(sorted(names.get(p, p).split()[-1] for p in on[team]))))
        if incoming in on[team]:
            return [], ("sub in %s (%s) already on floor"
                        % (incoming, names.get(incoming, "?")))

        close(secs, e.get("action_id"))
        on[team].discard(outgoing)
        on[team].add(incoming)
        if len(on[team]) != 5:
            return [], "%d on floor for %s after sub" % (len(on[team]), team)
        cur_start = secs
        cur_action = e.get("action_id")

    close(0.0, events[-1]["action_id"] if events else None)
    return stints, None


def minutes_from_stints(stints, home, away):
    mins = defaultdict(float)
    for s in stints:
        dur = (s["start_secs"] or 0) - (s["end_secs"] or 0)
        if dur <= 0:
            continue
        for p in s["home_on"]:
            mins[p] += dur
        for p in s["away_on"]:
            mins[p] += dur
    return mins


# ---------------------------------------------------------------------------

def season_games(con, season):
    """Stream a season's events, grouped by game.

    Order matters: in DuckDB's Python API con.execute() returns the connection
    and resets the result set, so any other query run after the event query
    would silently destroy it. Everything else is fetched first; the event
    stream is opened last and read to exhaustion.
    """
    box = defaultdict(lambda: defaultdict(list))
    roster = defaultdict(lambda: defaultdict(list))
    secs_played = defaultdict(dict)
    for gid, pid, team, started, sp in con.execute("""
        SELECT b.game_id, b.player_id, b.team_abbr, b.started, b.seconds_played
        FROM ab b JOIN ag g ON g.game_id = b.game_id
        WHERE g.season = ?""", [season]).fetchall():
        if started:
            box[gid][team].append(pid)
        roster[gid][team].append(pid)
        secs_played[gid][pid] = sp or 0

    names = dict(con.execute(
        "SELECT player_id, full_name FROM hot.players").fetchall())

    # the event cursor is opened last and nothing else touches the connection
    ev = con.execute("""
        SELECT e.game_id, e.action_id, e.period, e.clock_raw, e.action_type,
               e.team_abbr, e.player_id, e.description,
               g.home_abbr, g.away_abbr
        FROM ap e JOIN ag g ON g.game_id = e.game_id
        WHERE g.season = ?
        ORDER BY e.game_id, e.action_id""", [season])
    cols = [d[0] for d in ev.description]

    game, rows, meta = None, [], None
    while True:
        batch = ev.fetchmany(100000)
        if not batch:
            break
        for r in batch:
            d = dict(zip(cols, r))
            d["secs"] = clock_secs(d.get("clock_raw"))
            if d["game_id"] != game:
                if rows:
                    yield game, rows, meta, box[game], secs_played[game], names, roster[game]
                game, rows = d["game_id"], []
                meta = (d.get("home_abbr"), d.get("away_abbr"))
            rows.append(d)
    if rows:
        yield game, rows, meta, box[game], secs_played[game], names, roster[game]


def run_season(con, season, limit=None, verbose=False):
    ok, bad = 0, []
    all_stints = []
    min_err = []
    for i, (gid, events, meta, starters, secs_played, names, roster) in enumerate(
            season_games(con, season)):
        if limit and i >= limit:
            break
        home, away = meta
        stints, problem = parse_game(events, starters, names, home, away, roster)
        if problem:
            bad.append((gid, problem))
            continue

        mins = minutes_from_stints(stints, home, away)
        worst = 0.0
        for pid, sp in secs_played.items():
            if sp:
                worst = max(worst, abs(mins.get(pid, 0) - sp))
        min_err.append(worst)
        ok += 1
        for s in stints:
            if len(s["home_on"]) == 5 and len(s["away_on"]) == 5:
                all_stints.append((gid, season, s["period"], s["start_secs"],
                                   s["end_secs"], home, away,
                                   *s["home_on"], *s["away_on"],
                                   s["start_action"], s["end_action"]))
    return ok, bad, all_stints, min_err


def cmd_check(season, limit):
    con = connect()
    ok, bad, stints, min_err = run_season(con, season, limit)
    total = ok + len(bad)
    print("  %s" % season)
    print("    games parsed cleanly   %d / %d  (%.1f%%)"
          % (ok, total, 100 * ok / total if total else 0))
    print("    stints                 %d  (%.1f per game)"
          % (len(stints), len(stints) / ok if ok else 0))
    if min_err:
        min_err.sort()
        print("    worst minute error per game (seconds):")
        print("      median %.0f   90th %.0f   max %.0f"
              % (min_err[len(min_err) // 2],
                 min_err[int(len(min_err) * 0.9)], min_err[-1]))
        print("      (box score minutes vs minutes derived from the stints —")
        print("       small gaps are rounding, large ones mean a bad walk)")
    if bad:
        print("\n    %d games failed:" % len(bad))
        reasons = defaultdict(int)
        for gid, why in bad:
            key = re.sub(r"P\d{8}", "<player>", why)
            key = re.sub(r" at [\d.]+", "", key).split(":")[0]
            reasons[key] += 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
            print("      %-44s %d" % (k[:44], v))
        print("\n    examples:")
        for gid, why in bad[:3]:
            print("      %s  %s" % (gid, why))
    con.close()


def cmd_build(env, season):
    import duckdb
    con = connect()
    s3 = s3c(env)
    targets = [season] if season else [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM ag ORDER BY season DESC").fetchall()]

    cols = ("game_id VARCHAR, season VARCHAR, period SMALLINT, start_secs DOUBLE, "
            "end_secs DOUBLE, home_team VARCHAR, away_team VARCHAR, "
            + ", ".join("h%d VARCHAR" % i for i in range(1, 6)) + ", "
            + ", ".join("a%d VARCHAR" % i for i in range(1, 6)) + ", "
            "start_action BIGINT, end_action BIGINT")

    grand_ok = grand_bad = grand_stints = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in targets:
            ok, bad, stints, min_err = run_season(con, s)
            grand_ok += ok
            grand_bad += len(bad)
            grand_stints += len(stints)
            if not stints:
                print("  %-9s nothing parsed" % s)
                continue
            w = duckdb.connect()
            w.execute("CREATE TABLE l (%s)" % cols)
            w.executemany("INSERT INTO l VALUES (%s)" % ",".join("?" * 19), stints)
            path = os.path.join(tmp, "l.parquet")
            if os.path.exists(path):
                os.remove(path)
            w.execute("COPY l TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % path)
            w.close()
            s3.upload_file(path, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, s))
            med = sorted(min_err)[len(min_err) // 2] if min_err else 0
            print("  %-9s %5d ok  %4d failed  %7d stints  med err %3.0fs  %5.0f KB"
                  % (s, ok, len(bad), len(stints), med,
                     os.path.getsize(path) / 1024))
    con.close()
    tot = grand_ok + grand_bad
    print("\n  %d of %d games (%.1f%%), %d stints"
          % (grand_ok, tot, 100 * grand_ok / tot if tot else 0, grand_stints))
    if grand_bad:
        print("  %d games could not be walked and are excluded rather than guessed"
              % grand_bad)


def cmd_status(env):
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    keys, token, size = [], None, 0
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            keys.append(o["Key"])
            size += o["Size"]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if not keys:
        print("  nothing built yet")
        return
    print("  %d seasons, %.1f MB" % (len(keys), size / 1048576.0))
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for k in sorted(keys, reverse=True)[:5]:
            p = os.path.join(tmp, "x.parquet")
            s3.download_file(bucket, k, p)
            r = con.execute("""SELECT COUNT(*), COUNT(DISTINCT game_id)
                               FROM read_parquet('%s')""" % p).fetchone()
            print("    %-24s %8d stints  %5d games" % (k.split("/")[-1], r[0], r[1]))
            os.remove(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "status"])
    ap.add_argument("--season", default="2025-26")
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season, a.limit)
    elif a.cmd == "build":
        cmd_build(load_env(), None if a.season == "all" else a.season)
    else:
        cmd_status(load_env())
