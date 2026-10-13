#!/usr/bin/env python3
"""
cdn_feed.py — NBA's CDN play-by-play and box scores (cdn.nba.com liveData).

GitHub's servers cannot reach stats.nba.com; they can reach this. It carries
every field the pipeline reads, plus the assister, stealer and blocker by
player id. Coverage starts in 2019-20, so it serves the current season, not
the archive.

The two feeds label events differently ("3pt" against "Made Shot", "1 of 2"
against "Free Throw 1 of 2"). translate() rewrites a CDN game into the shape
stats.nba.com's playbyplayv3 uses, so ingest_pbp.py's parser and everything
downstream — possessions, turnover types, zones, assists — read it unchanged.

    python3 cdn_feed.py check [--n 12]    # Mac: CDN vs stats.nba.com, game by game
    python3 cdn_feed.py selftest          # anywhere: one game, totals must reconcile
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
RAW = os.path.join(HOME, "raw", "pbp_cdn")
V3_RAW = os.path.join(HOME, "raw", "pbp")
URL = "https://cdn.nba.com/static/json/liveData/%s/%s_%s.json"
# the CDN refuses requests that do not look like they came from nba.com
HEADERS = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
           "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9"}


# ------------------------------------------------------------------ fetching

def _get(kind, gid):
    from curl_cffi import requests as cr
    for attempt in range(3):
        try:
            r = cr.get(URL % (kind, kind, gid), headers=HEADERS,
                       impersonate="chrome", timeout=30)
        except Exception:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 404):      # game not published (yet)
            return None
        time.sleep(5 * (attempt + 1))
    return None


def _cache(gid, kind):
    return os.path.join(RAW, "%s_%s.json.gz" % (gid, kind))


def load(gid, kind, fetch=True):
    """kind 'pbp' or 'box'. Cached on disk; fetched when missing."""
    p = _cache(gid, kind)
    if os.path.exists(p):
        return json.loads(gzip.open(p, "rt", encoding="utf-8").read())
    if not fetch:
        return None
    d = _get("playbyplay" if kind == "pbp" else "boxscore", gid)
    if d:
        os.makedirs(RAW, exist_ok=True)
        gzip.open(p, "wt", encoding="utf-8").write(json.dumps(d))
    return d


def fetch_translated(gid):
    """For ingest_pbp.py: the game as playbyplayv3-shaped JSON text, or None."""
    pbp, box = load(gid, "pbp"), load(gid, "box")
    if not pbp or not box:
        return None
    acts = translate(pbp, box)
    if not acts:
        return None
    return json.dumps({"game": {"gameId": gid, "source": "cdn", "actions": acts}})


# --------------------------------------------------------------- translation

TOV = {
    "bad pass": "Bad Pass", "lost ball": "Lost Ball",
    "offensive foul": "Offensive Foul Turnover", "shot clock": "Shot Clock Turnover",
    "traveling": "Traveling", "travel": "Traveling",
    "double dribble": "Double Dribble", "palming": "Palming Turnover",
    "discontinued dribble": "Discontinue Dribble Turnover",
    "backcourt": "Backcourt Turnover", "3 second": "3 Second Violation",
    "5 second": "5 Second Violation", "8 second": "8 Second Violation",
    "offensive goaltending": "Offensive Goaltending",
    "step out of bounds": "Step Out of Bounds Turnover",
    "kicked ball": "Kicked Ball Violation", "lane violation": "Lane Violation Turnover",
    "jumpball violation": "Jump Ball Violation", "jump ball violation": "Jump Ball Violation",
    "basket from below": "Basket from Below Turnover",
    "illegal assist": "Illegal Assist Turnover",
    "too many players": "Too Many Players Turnover",
}
SIMPLE = {"rebound": "Rebound", "substitution": "Substitution", "jumpball": "Jump Ball",
          "timeout": "Timeout", "violation": "Violation", "period": "period",
          "steal": "Steal", "block": "Block", "ejection": "Ejection",
          "instantreplay": "Instant Replay"}
SHOT_BASE = {"layup": "Layup Shot", "dunk": "Dunk Shot", "hook": "Hook Shot",
             "jump shot": "Jump Shot", "fadeaway": "Fadeaway Jump Shot",
             "tip": "Tip Shot"}


def _title(s):
    return " ".join(w[:1].upper() + w[1:] for w in (s or "").split())


def tov_sub(st, desc):
    st, desc = (st or "").lower(), (desc or "").lower()
    if st == "out-of-bounds":
        if "pass" in desc:
            return "Out of Bounds - Bad Pass Turnover"
        if "step" in desc:
            return "Step Out of Bounds Turnover"
        return "Out of Bounds Lost Ball Turnover"
    return TOV.get(st) or TOV.get(_tov_key(st)) or _title(st) or "Unknown"


def _tov_key(st):
    """'3-second-violation' -> '3 second', 'offensive-kicked-ball' -> 'kicked ball'."""
    k = " ".join(st.replace("-", " ").split())
    if k.startswith("offensive ") and k != "offensive foul" \
            and k != "offensive goaltending":
        k = k[len("offensive "):]
    if k.endswith(" violation") and k not in ("lane violation", "jumpball violation"):
        k = k[: -len(" violation")]
    return k


def shot_sub(st, desc):
    base = SHOT_BASE.get((st or "").lower(), _title(st))
    return (_title(desc) + " " + base).strip() if desc else base


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def translate(pbp, box):
    """CDN actions -> playbyplayv3-shaped dicts, in game order."""
    home_id = (box.get("game") or {}).get("homeTeam", {}).get("teamId")
    acts = sorted((pbp.get("game") or {}).get("actions") or [],
                  key=lambda a: (a.get("orderNumber") or 0, a.get("actionNumber") or 0))
    pts = defaultdict(int)
    out = []
    for a in acts:
        t = (a.get("actionType") or "").lower()
        st = a.get("subType") or ""
        desc = a.get("descriptor") or ""
        if t == "game":
            continue
        res = a.get("shotResult") or ""
        pid = a.get("personId") or 0
        tid = a.get("teamId") or 0
        value = None
        total = None
        if t in ("2pt", "3pt"):
            at = "Made Shot" if res == "Made" else "Missed Shot"
            sub = shot_sub(st, desc)
            value = 3 if t == "3pt" else 2
            if res == "Made":
                pts[pid] += value
                total = pts[pid]
        elif t == "freethrow":
            at = "Free Throw"
            sub = "Free Throw " + (st if re.search(r"\d+ of \d+", st) else _title(st))
            if res == "Made":
                pts[pid] += 1
                total = pts[pid]
        elif t == "turnover":
            at, sub = "Turnover", tov_sub(st, desc)
        elif t == "foul":
            at, sub = "Foul", _title(desc) or _title(st)
        elif t == "period":
            at, sub = "period", st            # v3 writes "start" / "end"
        else:
            at, sub = SIMPLE.get(t, _title(t)), _title(st)
        out.append({
            "actionId": a.get("orderNumber") or a.get("actionNumber"),
            "actionNumber": a.get("actionNumber"),
            "period": a.get("period"),
            "clock": a.get("clock"),
            "personId": pid,
            "playerName": a.get("playerName") or "",
            "teamId": tid,
            "teamTricode": a.get("teamTricode") or "",
            "actionType": at,
            "subType": sub,
            "description": a.get("description") or "",
            "shotResult": res if t in ("2pt", "3pt", "freethrow") else "",
            "shotValue": value or 0,
            "location": ("h" if tid == home_id else "v") if tid else "",
            "shotDistance": a.get("shotDistance") if t in ("2pt", "3pt") else "",
            "xLegacy": a.get("xLegacy") if t in ("2pt", "3pt") else "",
            "yLegacy": a.get("yLegacy") if t in ("2pt", "3pt") else "",
            "isFieldGoal": 1 if t in ("2pt", "3pt") else 0,
            "pointsTotal": total,
            "scoreHome": a.get("scoreHome"),
            "scoreAway": a.get("scoreAway"),
            # CDN extras, kept for later use (lineups, possession, assister)
            "assistPersonId": a.get("assistPersonId"),
            "stealPersonId": a.get("stealPersonId"),
            "blockPersonId": a.get("blockPersonId"),
            "possession": a.get("possession"),
            "qualifiers": a.get("qualifiers"),
        })
    return out


# -------------------------------------------- stand-ins for the stats.nba.com
# leaguegamelog calls in bridge_games.py and bridge_nba_ids.py

_GAMES = None          # [(nba_game_id, season, game_date, home, away)]
TYPE_DIGIT = {"Regular Season": "2", "Playoffs": "4", "PlayIn": "5", "IST": "6"}


def preload(source):
    """Read what the stand-ins need before the caller opens its own connection."""
    global _GAMES
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    if source == "schedule":
        _GAMES = con.execute("""SELECT nba_game_id, season, CAST(game_date AS VARCHAR),
                                       home_abbr, away_abbr FROM scheduled_games""").fetchall()
    else:
        _GAMES = con.execute("""SELECT source_game_id, season, CAST(game_date AS VARCHAR),
                                       NULL, NULL FROM game_identifiers
                                WHERE source = 'nba'""").fetchall()
    con.close()


def schedule_log(season, season_type, force=False):
    """bridge_games.fetch_log, from the CDN schedule: two team rows per game."""
    digit = TYPE_DIGIT.get(season_type)
    rows = []
    for gid, s, d, home, away in _GAMES or []:
        if s == season and digit and gid[2:3] == digit and home and away:
            rows.append({"GAME_ID": gid, "TEAM_ABBREVIATION": home, "GAME_DATE": d})
            rows.append({"GAME_ID": gid, "TEAM_ABBREVIATION": away, "GAME_DATE": d})
    return rows


def _min(raw):
    m = re.match(r"PT(\d+)M([\d.]+)S", raw or "")
    return "%d:%02d" % (int(m.group(1)), int(float(m.group(2)))) if m else None


def player_log(season, season_type, force=False):
    """bridge_nba_ids.fetch_gamelog, from CDN box scores already on disk.

    Only games fetched to this machine are used: on GitHub that is tonight's
    games, which is where any unbridged player is. Every season type comes
    back under "Regular Season" so no game is counted twice."""
    if season_type != "Regular Season":
        return []
    rows = []
    for gid, s, d, _, _ in _GAMES or []:
        if s != season:
            continue
        box = load(gid, "box", fetch=False)
        if not box:
            continue
        for side in ("homeTeam", "awayTeam"):
            team = box["game"].get(side) or {}
            for p in team.get("players") or []:
                if str(p.get("played")) != "1":
                    continue
                stt = p.get("statistics") or {}
                rows.append({
                    "GAME_ID": gid, "GAME_DATE": d,
                    "PLAYER_ID": p.get("personId"), "PLAYER_NAME": p.get("name"),
                    "TEAM_ABBREVIATION": team.get("teamTricode"),
                    "MIN": _min(stt.get("minutes")),
                    "PTS": stt.get("points"), "REB": stt.get("reboundsTotal"),
                    "AST": stt.get("assists"), "FGA": stt.get("fieldGoalsAttempted"),
                    "FTA": stt.get("freeThrowsAttempted"),
                })
    return rows


# ------------------------------------------------------------------- checks

AST_RE = re.compile(r"\(([^()]*?)\s+\d+\s+AST\)")


def events_for_parser(acts):
    """v3-shaped dicts -> the rows parse_possessions.parse_game reads."""
    from parse_possessions import clock_secs
    out = []
    for a in acts:
        team = a.get("teamTricode") or None
        out.append({
            "action_id": _int(a.get("actionId")), "period": _int(a.get("period")),
            "clock_raw": a.get("clock"), "secs": clock_secs(a.get("clock")),
            "action_type": a.get("actionType") or None, "sub_type": a.get("subType") or None,
            "team_abbr": team, "shot_result": a.get("shotResult") or None,
            "shot_value": _int(a.get("shotValue")) or None,
            "description": a.get("description") or None,
            "person": _int(a.get("personId")),
        })
    out.sort(key=lambda e: (e["action_id"] or 0))
    return out


def tallies(evs):
    """Per player: FGM, FGA, 3PM, 3PA, FTM, FTA, TOV, plus team points."""
    from parse_possessions import ft_made
    t = defaultdict(Counter)
    for e in evs:
        at, p = (e["action_type"] or "").lower(), e["person"]
        if at in ("made shot", "missed shot"):
            t[p]["fga"] += 1
            t[p]["fgm"] += at == "made shot"
            if e["shot_value"] == 3:
                t[p]["3pa"] += 1
                t[p]["3pm"] += at == "made shot"
        elif at == "free throw":
            t[p]["fta"] += 1
            t[p]["ftm"] += ft_made(e)
        elif at == "turnover" and p and not (1610612700 <= p <= 1610612800):
            t[p]["tov"] += 1
    return t


def possessions(evs):
    from parse_possessions import parse_game
    rows = parse_game(evs)
    return len(rows), sum(r["points"] for r in rows)


def box_points(box):
    g = box["game"]
    return g["homeTeam"]["score"] + g["awayTeam"]["score"]


def cmd_selftest(gid):
    pbp, box = _get("playbyplay", gid), _get("boxscore", gid)
    if not pbp or not box:
        sys.exit("  could not fetch %s from the CDN" % gid)
    acts = translate(pbp, box)
    evs = events_for_parser(acts)
    n, pts = possessions(evs)
    made = [a for a in acts if a["actionType"] == "Made Shot"]
    print("  %s: %d events, %d possessions, %d points from possessions, box score %d %s"
          % (gid, len(acts), n, pts, box_points(box),
             "OK" if pts == box_points(box) else "MISMATCH"))
    print("  made FG %d, assisted by id %d, assister in description %d"
          % (len(made), sum(1 for a in made if a["assistPersonId"]),
             sum(1 for a in made if AST_RE.search(a["description"]))))


def v3_actions(gid):
    """stats.nba.com play-by-play: the local cache, else fetched (Mac only)."""
    p = os.path.join(V3_RAW, gid[:5], "%s.json.gz" % gid)
    if os.path.exists(p):
        return json.loads(gzip.open(p, "rt", encoding="utf-8").read())["game"]["actions"]
    from curl_cffi import requests as cr
    h = dict(HEADERS, **{"x-nba-stats-origin": "stats", "x-nba-stats-token": "true"})
    try:
        r = cr.get("https://stats.nba.com/stats/playbyplayv3?GameID=%s"
                   "&StartPeriod=0&EndPeriod=14" % gid, headers=h,
                   impersonate="chrome", timeout=30)
        return r.json()["game"]["actions"] if r.status_code == 200 else None
    except Exception:
        return None


def cmd_check(n):
    import duckdb
    import random
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""SELECT source_game_id FROM game_identifiers
                          WHERE source='nba' AND season='2025-26'""").fetchall()
    con.close()
    have = [g for (g,) in rows]
    random.seed(7)
    sample = random.sample(have, min(n, len(have)))
    print("  %d 2025-26 games bridged; checking %d\n" % (len(have), len(sample)))
    print("  %-11s %9s %9s %10s %10s %6s %6s  %s"
          % ("game", "poss v3", "poss cdn", "pts v3", "pts cdn", "box", "lines", "x/y same"))
    tov_conf, shot_same, shot_n = Counter(), 0, 0
    ast = Counter()
    bad_lines = []
    for gid in sample:
        v3 = v3_actions(gid)
        if not v3:
            print("  %-11s no stats.nba.com play-by-play" % gid)
            continue
        pbp, box = load(gid, "pbp"), load(gid, "box")
        if not pbp or not box:
            print("  %-11s not on the CDN" % gid)
            continue
        live = translate(pbp, box)
        ev3, evl = events_for_parser(v3), events_for_parser(live)
        n3, p3 = possessions(ev3)
        nl, pl = possessions(evl)
        t3, tl = tallies(ev3), tallies(evl)
        diff = [(p, dict(t3.get(p, {})), dict(tl.get(p, {})))
                for p in set(t3) | set(tl)
                if p and not (1610612700 <= p <= 1610612800)
                and {k: v for k, v in t3.get(p, {}).items() if v}
                != {k: v for k, v in tl.get(p, {}).items() if v}]
        bad_lines += [(gid,) + d for d in diff]
        v3n = {a.get("actionNumber"): a for a in v3 if a.get("isFieldGoal") == 1}
        same = tot = 0
        for a in live:
            b = v3n.get(a["actionNumber"])
            if a["isFieldGoal"] and b:
                tot += 1
                same += (a["xLegacy"], a["yLegacy"]) == (b.get("xLegacy"), b.get("yLegacy"))
        shot_same += same
        shot_n += tot
        v3t = {a.get("actionNumber"): a.get("subType") for a in v3
               if (a.get("actionType") or "").lower() == "turnover"}
        for a in live:
            if a["actionType"] == "Turnover" and a["actionNumber"] in v3t:
                tov_conf[(a["subType"], v3t[a["actionNumber"]])] += 1
        ast["v3 made"] += sum(1 for a in v3 if a.get("actionType") == "Made Shot")
        ast["v3 assisted"] += sum(1 for a in v3 if a.get("actionType") == "Made Shot"
                                  and AST_RE.search(a.get("description") or ""))
        ast["cdn made"] += sum(1 for a in live if a["actionType"] == "Made Shot")
        ast["cdn assisted (id)"] += sum(1 for a in live if a["actionType"] == "Made Shot"
                                        and a["assistPersonId"])
        ast["cdn assisted (text)"] += sum(1 for a in live if a["actionType"] == "Made Shot"
                                          and AST_RE.search(a["description"]))
        print("  %-11s %9d %9d %10d %10d %6d %6s  %d/%d"
              % (gid, n3, nl, p3, pl, box_points(box),
                 "same" if not diff else "%d diff" % len(diff), same, tot))
        time.sleep(0.3)

    print("\n  shot coordinates identical: %d/%d" % (shot_same, shot_n))
    print("  assists: %s" % dict(ast))
    print("\n  turnover types  cdn (translated) -> stats.nba.com   count")
    for (a, b), c in tov_conf.most_common():
        print("    %s %-36s -> %-36s %4d" % ("  " if a == b else "!!", a, b, c))
    if bad_lines:
        print("\n  player lines that differ (first 12): game, nba id, stats.nba.com, cdn")
        for g, p, a, b in bad_lines[:12]:
            print("    %s %s  %s  %s" % (g, p, a, b))


if __name__ == "__main__":
    sys.path.insert(0, HOME)
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "selftest"])
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--game", default="0022500001")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.n)
    else:
        cmd_selftest(a.game)
