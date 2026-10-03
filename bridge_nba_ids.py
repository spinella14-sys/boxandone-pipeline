#!/usr/bin/env python3
"""
bridge_nba_ids.py — map BBRef player IDs to NBA.com person IDs.

Never matches on names. Names are ambiguous (nine same-name-same-season pairs
already exist in the registry). Instead:

  1. Pair games by date + normalized team abbreviations.
  2. Inside a paired game, fingerprint each player on their full box line:
     seconds, pts, trb, ast, fga, fta.
  3. A player who is unambiguous in ANY single game is mapped permanently.
     Ambiguity in one game resolves by majority vote across the season.

Uses leaguegamelog: one request returns every player-game line for a whole
season, so the initial build is a handful of requests, not thousands.

    python3 bridge_nba_ids.py build   --season 2025-26
    python3 bridge_nba_ids.py update              # incremental, for nightly cron
    python3 bridge_nba_ids.py status

Requires: nba_api  (pip3 install nba_api)
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE_DIR = os.path.join(HOME, "raw", "nba_gamelogs")

# BBRef and NBA.com disagree on these. Normalize both sides to a common key.
TEAM_ALIASES = {
    "BRK": "BKN", "BKN": "BKN",
    "PHO": "PHX", "PHX": "PHX",
    "CHO": "CHA", "CHA": "CHA", "CHH": "CHA",
    "NOH": "NOP", "NOK": "NOP", "NOP": "NOP",
    "SEA": "OKC", "OKC": "OKC",
    "VAN": "MEM", "MEM": "MEM",
    "NJN": "BKN",
    "WSB": "WAS", "WAS": "WAS",
    "SDC": "LAC", "LAC": "LAC",
    "KCK": "SAC", "SAC": "SAC",
}

# Exact integers on BOTH sides. "sec" is deliberately absent: BBRef stores
# exact seconds (35:12 -> 2112) while NBA.com rounds to whole minutes
# (35 -> 2100), so any fingerprint containing it never matches.
FINGERPRINT = ("pts", "trb", "ast", "fga", "fta")
MINUTE_TOLERANCE_SEC = 90      # tiebreak only
MINUTE_SEPARATION_SEC = 30     # runner-up must be at least this much worse


def norm_team(abbr):
    if not abbr:
        return None
    a = abbr.strip().upper()
    return TEAM_ALIASES.get(a, a)


def db():
    import duckdb
    return duckdb.connect(DB_PATH)


# ---------------------------------------------------------------------------
# NBA.com side
# ---------------------------------------------------------------------------

def fetch_gamelog(season, season_type, force=False):
    """One request -> every player-game line for a season. Cached to disk.

    Uses curl_cffi: stats.nba.com fingerprints TLS and drops plain clients.
    Cache filename carries a _players suffix so it cannot collide with
    bridge_games.py's team-level cache for the same season and type.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    tag = season_type.replace(" ", "_")
    path = os.path.join(CACHE_DIR, "%s_%s_players.json" % (season, tag))

    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    from curl_cffi import requests as cr

    url = ("https://stats.nba.com/stats/leaguegamelog"
           "?Counter=0&Season=%s&SeasonType=%s&PlayerOrTeam=P"
           "&Direction=DESC&Sorter=DATE&LeagueID=00&DateFrom=&DateTo="
           % (season, season_type.replace(" ", "+")))
    headers = {
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
        "Accept": "application/json, text/plain, */*",
    }

    for attempt in range(4):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s %s: %s" % (season, season_type, str(e)[:60]))
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code != 200:
            print("      %s %s: HTTP %s" % (season, season_type, r.status_code))
            time.sleep(15 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except Exception:
            time.sleep(15)
            continue

        sets = payload.get("resultSets") or []
        if not sets or not sets[0].get("rowSet"):
            # season type does not exist for this season (e.g. PlayIn pre-2020)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []

        hdr = sets[0]["headers"]
        rows = [dict(zip(hdr, row)) for row in sets[0]["rowSet"]]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        print("    %s %s: %d player-game rows" % (season, season_type, len(rows)))
        time.sleep(2.5)
        return rows
    return []

def nba_rows_by_game(raw):
    """Group NBA.com rows into {(date, frozenset(teams)): [player rows]}."""
    by_gid = defaultdict(list)
    meta = {}
    for r in raw:
        gid = r.get("GAME_ID")
        if not gid:
            continue
        mp = r.get("MIN")
        sec = None
        if mp is not None:
            try:
                if isinstance(mp, str) and ":" in mp:
                    m, s = mp.split(":")
                    sec = int(m) * 60 + int(s)
                else:
                    sec = int(round(float(mp) * 60))
            except (ValueError, TypeError):
                sec = None

        by_gid[gid].append({
            "nba_id": r.get("PLAYER_ID"),
            "name": r.get("PLAYER_NAME"),
            "team": norm_team(r.get("TEAM_ABBREVIATION")),
            "sec": sec,
            "pts": r.get("PTS"), "trb": r.get("REB"), "ast": r.get("AST"),
            "fga": r.get("FGA"), "fta": r.get("FTA"),
        })
        meta.setdefault(gid, r.get("GAME_DATE"))

    keyed = {}
    for gid, rows in by_gid.items():
        date = (meta.get(gid) or "")[:10]
        teams = frozenset(x["team"] for x in rows if x["team"])
        if date and len(teams) == 2:
            keyed[(date, teams)] = {"nba_game_id": gid, "players": rows}
    return keyed


# ---------------------------------------------------------------------------
# Our side
# ---------------------------------------------------------------------------

def our_rows_by_game(con, season):
    q = """
        SELECT g.game_id, CAST(g.game_date AS VARCHAR) AS d,
               b.player_id, b.team_abbr, b.seconds_played,
               b.pts, b.trb, b.ast, b.fga, b.fta
        FROM player_game_box b
        JOIN games g ON g.game_id = b.game_id
        WHERE g.season = ? AND b.played
    """
    out = defaultdict(list)
    dates = {}
    for r in con.execute(q, [season]).fetchall():
        gid, d, pid, team, sec, pts, trb, ast, fga, fta = r
        out[gid].append({
            "player_id": pid, "team": norm_team(team), "sec": sec,
            "pts": pts, "trb": trb, "ast": ast, "fga": fga, "fta": fta,
        })
        dates[gid] = d

    keyed = {}
    for gid, rows in out.items():
        teams = frozenset(x["team"] for x in rows if x["team"])
        if len(teams) == 2:
            keyed[(dates[gid], teams)] = {"game_id": gid, "players": rows}
    return keyed


# ---------------------------------------------------------------------------
# Fingerprint matching
# ---------------------------------------------------------------------------

def fp(row):
    return tuple(row.get(k) for k in FINGERPRINT)


def _mins(row):
    s = row.get("sec")
    return None if s is None else s / 60.0


def match_game(ours, theirs):
    """
    Return (mappings, ambiguous_count).

    Primary key is the stat line, which is exact on both sides. When several
    players on a team share a line, fall back to minutes: pick the closest
    counterpart, and only accept when it is clearly closest.
    """
    mappings = []
    ambiguous = 0

    for team in {p["team"] for p in ours["players"]}:
        a = [p for p in ours["players"] if p["team"] == team]
        b = [p for p in theirs["players"] if p["team"] == team]
        if not a or not b:
            continue

        a_idx, b_idx = defaultdict(list), defaultdict(list)
        for p in a:
            a_idx[fp(p)].append(p)
        for p in b:
            b_idx[fp(p)].append(p)

        for key, alist in a_idx.items():
            blist = b_idx.get(key, [])

            if len(alist) == 1 and len(blist) == 1:
                mappings.append((alist[0]["player_id"], blist[0]["nba_id"],
                                 blist[0]["name"]))
                continue

            if not blist:
                ambiguous += len(alist)
                continue

            resolved = 0
            for ap in alist:
                am = _mins(ap)
                if am is None:
                    continue
                scored = []
                for bp in blist:
                    bm = _mins(bp)
                    if bm is not None:
                        scored.append((abs(am - bm) * 60.0, bp))
                if not scored:
                    continue
                scored.sort(key=lambda x: x[0])
                best_d, best_p = scored[0]
                second_d = scored[1][0] if len(scored) > 1 else None
                if best_d <= MINUTE_TOLERANCE_SEC and (
                        second_d is None or second_d >= best_d + MINUTE_SEPARATION_SEC):
                    mappings.append((ap["player_id"], best_p["nba_id"],
                                     best_p["name"]))
                    resolved += 1
            ambiguous += len(alist) - resolved

    return mappings, ambiguous


def resolve(season, con, seasons_types=("Regular Season", "Playoffs", "PlayIn", "IST", "Showcase", "Pre Season")):
    ours_all = our_rows_by_game(con, season)
    if not ours_all:
        print(f"    no local games for {season}")
        return {}, 0, 0

    theirs_all = {}
    for st in seasons_types:
        raw = fetch_gamelog(season, st)
        theirs_all.update(nba_rows_by_game(raw))

    votes = defaultdict(lambda: defaultdict(int))
    names = {}
    paired = unpaired = ambiguous = 0

    for key, ours in ours_all.items():
        theirs = theirs_all.get(key)
        if not theirs:
            unpaired += 1
            continue
        paired += 1
        maps, amb = match_game(ours, theirs)
        ambiguous += amb
        for pid, nid, nm in maps:
            votes[pid][nid] += 1
            names[nid] = nm

    final = {}
    conflicts = 0
    for pid, counts in votes.items():
        best = max(counts.items(), key=lambda x: x[1])
        if len(counts) > 1:
            conflicts += 1
            total = sum(counts.values())
            if best[1] / total < 0.80:
                continue          # too contested — leave unmapped
        final[pid] = (best[0], names.get(best[0]), best[1])

    print(f"    games paired {paired:,} | unpaired {unpaired} | "
          f"ambiguous player-games {ambiguous:,} | vote conflicts {conflicts}")
    return final, paired, unpaired


def write_ids(con, mapping):
    written = skipped = 0
    for pid, (nid, nm, votes) in mapping.items():
        exists = con.execute(
            "SELECT player_id FROM player_identifiers WHERE source='nba' AND source_id=?",
            [str(nid)]).fetchone()
        if exists:
            if exists[0] != pid:
                print(f"    CONFLICT nba:{nid} already -> {exists[0]}, now claims {pid}")
            skipped += 1
            continue
        already = con.execute(
            "SELECT source_id FROM player_identifiers WHERE source='nba' AND player_id=?",
            [pid]).fetchone()
        if already:
            skipped += 1
            continue

        con.execute("""
            INSERT INTO player_identifiers
              (source, source_id, player_id, is_primary, confidence, linked_by, provenance)
            VALUES ('nba', ?, ?, TRUE, ?, 'bridge_nba_ids', 'derived')
        """, [str(nid), pid, min(1.0, 0.5 + votes / 20.0)])
        written += 1
    con.commit()
    return written, skipped


# ---------------------------------------------------------------------------

def cmd_build(season):
    con = db()
    print(f"  building bridge for {season}")
    mapping, _, _ = resolve(season, con)
    w, s = write_ids(con, mapping)
    print(f"    mapped {len(mapping):,} players | wrote {w:,} | already present {s:,}")
    cmd_status(con)
    con.close()


def cmd_update():
    """Incremental: only players in recent games lacking an nba identifier."""
    con = db()
    missing = con.execute("""
        SELECT DISTINCT g.season, COUNT(DISTINCT b.player_id)
        FROM player_game_box b
        JOIN games g ON g.game_id = b.game_id
        WHERE b.played AND b.player_id NOT IN (
            SELECT player_id FROM player_identifiers WHERE source='nba')
        GROUP BY 1 ORDER BY 1 DESC
    """).fetchall()

    if not missing:
        print("  every player already bridged — nothing to do")
        con.close()
        return

    for season, n in missing:
        print(f"  {season}: {n} players unmapped")
        # refresh the current season's log; older seasons use cache
        mapping, _, _ = resolve(season, con)
        w, s = write_ids(con, mapping)
        print(f"    wrote {w:,}")
    cmd_status(con)
    con.close()


def cmd_status(con=None):
    close = con is None
    con = con or db()
    tot = con.execute("SELECT COUNT(DISTINCT player_id) FROM player_game_box").fetchone()[0]
    br = con.execute(
        "SELECT COUNT(*) FROM player_identifiers WHERE source='nba'").fetchone()[0]
    unmapped = con.execute("""
        SELECT p.full_name, COUNT(*) g, SUM(b.pts) pts
        FROM player_game_box b JOIN players p ON p.player_id=b.player_id
        WHERE b.played AND b.player_id NOT IN (
            SELECT player_id FROM player_identifiers WHERE source='nba')
        GROUP BY 1 ORDER BY g DESC LIMIT 12
    """).fetchall()
    print(f"\n  players with games: {tot:,} | bridged to NBA.com: {br:,}")
    if unmapped:
        print("  unmapped (most games first):")
        for r in unmapped:
            print(f"    {r[0]:28} {r[1]:>3} gp  {r[2] or 0:>5} pts")
    if close:
        con.close()


def cmd_diagnose(season):
    """Print one paired game side by side and list unpaired games."""
    con = db()
    ours_all = our_rows_by_game(con, season)
    theirs_all = {}
    for st in ("Regular Season", "Playoffs", "PlayIn", "IST", "Showcase", "Pre Season"):
        theirs_all.update(nba_rows_by_game(fetch_gamelog(season, st)))

    unpaired = [k for k in ours_all if k not in theirs_all]
    print(f"\n  local games {len(ours_all):,} | nba games {len(theirs_all):,} "
          f"| unpaired {len(unpaired)}")
    for k in sorted(unpaired)[:15]:
        print(f"    unpaired: {k[0]}  {sorted(k[1])}")

    key = next((k for k in ours_all if k in theirs_all), None)
    if not key:
        print("  no paired game to inspect")
        con.close()
        return

    ours, theirs = ours_all[key], theirs_all[key]
    team = sorted({p["team"] for p in ours["players"]})[0]
    print(f"\n  sample game {key[0]} {sorted(key[1])} — team {team}")
    print(f"  {'--- OURS (bbref) ---':>34}    {'--- THEIRS (nba.com) ---'}")
    print(f"  {'sec':>6}{'min':>8}{'pts':>5}{'trb':>5}{'ast':>5}{'fga':>5}"
          f"    {'sec':>6}{'min':>8}{'pts':>5}{'trb':>5}{'ast':>5}{'fga':>5}  name")

    a = sorted([p for p in ours["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    b = sorted([p for p in theirs["players"] if p["team"] == team],
               key=lambda x: -(x.get("sec") or 0))
    for i in range(max(len(a), len(b))):
        l = a[i] if i < len(a) else {}
        r = b[i] if i < len(b) else {}
        ls, rs = l.get("sec"), r.get("sec")
        print(f"  {str(ls):>6}{(ls/60 if ls else 0):>8.2f}"
              f"{str(l.get('pts')):>5}{str(l.get('trb')):>5}"
              f"{str(l.get('ast')):>5}{str(l.get('fga')):>5}"
              f"    {str(rs):>6}{(rs/60 if rs else 0):>8.2f}"
              f"{str(r.get('pts')):>5}{str(r.get('trb')):>5}"
              f"{str(r.get('ast')):>5}{str(r.get('fga')):>5}  {r.get('name','')}")

    whole = sum(1 for p in b if p.get("sec") is not None and p["sec"] % 60 == 0)
    tot = sum(1 for p in b if p.get("sec") is not None)
    verdict = "ROUNDED — diagnosis confirmed" if tot and whole == tot else "exact seconds"
    print(f"\n  nba.com rows landing on a whole minute: {whole}/{tot}  -> {verdict}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "update", "status", "diagnose"])
    ap.add_argument("--season", default="2025-26")
    ap.add_argument("--seasons", default=None,
                    help="range, newest first, e.g. 2024-25:1996-97")
    a = ap.parse_args()

    if a.cmd == "build":
        if a.seasons:
            hi, lo = a.seasons.split(":")
            hi_y, lo_y = int(hi.split("-")[0]), int(lo.split("-")[0])
            for y in range(hi_y, lo_y - 1, -1):
                cmd_build("%d-%s" % (y, str(y + 1)[2:]))
        else:
            cmd_build(a.season)
    elif a.cmd == "update":
        cmd_update()
    elif a.cmd == "diagnose":
        cmd_diagnose(a.season)
    else:
        cmd_status()
