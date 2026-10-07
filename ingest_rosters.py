#!/usr/bin/env python3
"""
ingest_rosters.py — who is on an NBA roster today, and who they are.

The registry was built from Basketball Reference box scores, so it contains
everyone who has played a regular-season NBA game and nobody who has not. Every
rookie, every camp invite, every two-way signing is therefore missing — which is
why a preseason box score has players in it that the database cannot name.

commonteamroster returns each team's current roster with jersey, position,
height, weight, BIRTHDATE and years of experience. Thirty requests covers the
league, and the birthdate is what makes identity resolvable rather than guessed:
a name alone is ambiguous, a name with a matching date of birth is not.

Resolution runs in three tiers, strongest first:

  the NBA id is already linked        nothing to decide
  name and birthdate both agree       linked automatically, and an alias is
                                      recorded so the next source resolves
                                      without asking
  anything weaker                     written to staging_players with a score
                                      and the reasons, for a person to approve

A player who matches nothing is created. In a registry holding thirty years of
NBA history, a name that appears nowhere is almost always genuinely new.

Rosters are stored dated rather than overwritten, so comparing two days shows
who moved — which is transaction history for free.

    python3 ingest_rosters.py check          # fetch and report, write nothing
    python3 ingest_rosters.py build
    python3 ingest_rosters.py roster --team BOS
"""

import argparse
import json
import os
import sys
import time
import unicodedata
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE_DIR = os.path.join(HOME, "raw", "rosters")
SEASON = "2026-27"

HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}

# the league's abbreviations against Basketball Reference's
NBA_TO_OURS = {"BKN": "BRK", "CHA": "CHO", "PHX": "PHO"}

# team id -> abbreviation, needed because commonteamroster takes an id
TEAMS = {
    1610612737: "ATL", 1610612738: "BOS", 1610612751: "BKN", 1610612766: "CHA",
    1610612741: "CHI", 1610612739: "CLE", 1610612742: "DAL", 1610612743: "DEN",
    1610612765: "DET", 1610612744: "GSW", 1610612745: "HOU", 1610612754: "IND",
    1610612746: "LAC", 1610612747: "LAL", 1610612763: "MEM", 1610612748: "MIA",
    1610612749: "MIL", 1610612750: "MIN", 1610612740: "NOP", 1610612752: "NYK",
    1610612760: "OKC", 1610612753: "ORL", 1610612755: "PHI", 1610612756: "PHX",
    1610612757: "POR", 1610612758: "SAC", 1610612759: "SAS", 1610612761: "TOR",
    1610612762: "UTA", 1610612764: "WAS",
}

DDL = """
CREATE TABLE IF NOT EXISTS team_rosters (
    as_of        DATE NOT NULL,
    season       VARCHAR NOT NULL,
    team_abbr    VARCHAR NOT NULL,
    player_id    VARCHAR NOT NULL,
    jersey       VARCHAR,
    position_raw VARCHAR,
    height_in    SMALLINT,
    weight_lb    SMALLINT,
    experience   VARCHAR,
    school       VARCHAR,
    source       VARCHAR NOT NULL DEFAULT 'nba',
    PRIMARY KEY (as_of, team_abbr, player_id)
)
"""


def norm(s):
    """Fold a name for comparison. Unicode decomposition rather than a hand
    written accent map, which is what broke on Şengün in the lineup parser."""
    s = unicodedata.normalize("NFKD", (s or "").lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("ø", "o"), ("ð", "d"), ("þ", "th"), ("æ", "ae"),
                 ("œ", "oe"), ("ł", "l"), ("đ", "d"), ("ß", "ss")):
        s = s.replace(a, b)
    s = s.replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(s.split())


def height_to_inches(v):
    if not v:
        return None
    s = str(v).strip()
    if "-" in s:
        try:
            ft, inch = s.split("-")
            return int(ft) * 12 + int(inch)
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def fetch_roster(team_id, season=SEASON, force=False):
    """One team's roster, cached to disk the way the gamelog fetch is."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "%s_%s.json" % (season, team_id))
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    from curl_cffi import requests as cr
    url = ("https://stats.nba.com/stats/commonteamroster"
           "?LeagueID=00&Season=%s&TeamID=%d" % (season, team_id))

    for attempt in range(4):
        try:
            r = cr.get(url, headers=HEADERS, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s: %s" % (team_id, str(e)[:60]))
            time.sleep(8 * (attempt + 1))
            continue
        if r.status_code != 200:
            print("      %s: HTTP %s" % (team_id, r.status_code))
            time.sleep(12 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except Exception:
            time.sleep(12)
            continue
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        time.sleep(1.2)
        return payload
    return None


def rows_of(payload):
    sets = (payload or {}).get("resultSets") or []
    if not sets:
        return []
    hdr = sets[0]["headers"]
    return [dict(zip(hdr, r)) for r in sets[0]["rowSet"]]


def parse_player(r, team_abbr):
    bd = r.get("BIRTH_DATE")
    birth = None
    if bd:
        for fmt in ("%b %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                birth = datetime.strptime(str(bd)[:19], fmt).date()
                break
            except ValueError:
                continue
    return {
        "nba_id": str(r.get("PLAYER_ID") or ""),
        "name": r.get("PLAYER") or "",
        "name_norm": norm(r.get("PLAYER")),
        "team_abbr": NBA_TO_OURS.get(team_abbr, team_abbr),
        "jersey": str(r.get("NUM") or "").strip() or None,
        "position_raw": r.get("POSITION") or None,
        "height_in": height_to_inches(r.get("HEIGHT")),
        "weight_lb": int(r["WEIGHT"]) if str(r.get("WEIGHT") or "").isdigit() else None,
        "birthdate": birth,
        "experience": str(r.get("EXP") or "").strip() or None,
        "school": r.get("SCHOOL") or None,
    }


def resolve(con, people):
    """Three tiers, strongest first. Returns per-person decisions."""
    linked = dict(con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'"
    ).fetchall())

    registry = con.execute("""
        SELECT p.player_id, p.name_normalized, p.birthdate,
               (SELECT COUNT(*) FROM player_identifiers i
                 WHERE i.player_id = p.player_id AND i.source='nba') AS has_nba
        FROM players p""").fetchall()
    by_name = {}
    for pid, nn, bd, has_nba in registry:
        by_name.setdefault(nn, []).append((pid, bd, has_nba))

    out = []
    for p in people:
        if p["nba_id"] in linked:
            out.append({**p, "decision": "already", "player_id": linked[p["nba_id"]]})
            continue

        cands = by_name.get(p["name_norm"], [])
        free = [c for c in cands if not c[2]]

        exact = [c for c in free
                 if p["birthdate"] and c[1] and c[1] == p["birthdate"]]
        if len(exact) == 1:
            out.append({**p, "decision": "link", "player_id": exact[0][0],
                        "score": 1.0, "why": "name and birthdate agree"})
            continue

        if len(free) == 1 and not p["birthdate"]:
            out.append({**p, "decision": "stage", "player_id": free[0][0],
                        "score": 0.6, "why": "name matches, no birthdate to confirm"})
            continue

        if len(free) > 1:
            out.append({**p, "decision": "stage", "player_id": free[0][0],
                        "score": 0.4,
                        "why": "%d players share this name" % len(free)})
            continue

        if cands and not free:
            out.append({**p, "decision": "stage", "player_id": cands[0][0],
                        "score": 0.3,
                        "why": "name matches someone already linked to another NBA id"})
            continue

        out.append({**p, "decision": "create", "player_id": None,
                    "score": 1.0, "why": "no one in the registry by this name"})
    return out


def next_ids(con, n):
    row = con.execute("SELECT MAX(CAST(SUBSTR(player_id,2) AS INTEGER)) FROM players"
                      ).fetchone()[0] or 0
    return ["P%08d" % (row + i + 1) for i in range(n)]


def cmd_check(con, season):
    people = []
    print("  fetching %d rosters…" % len(TEAMS))
    for tid, abbr in TEAMS.items():
        payload = fetch_roster(tid, season)
        for r in rows_of(payload):
            people.append(parse_player(r, abbr))
    print("  %d players on rosters\n" % len(people))

    decided = resolve(con, people)
    counts = {}
    for d in decided:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    for k in ("already", "link", "create", "stage"):
        print("  %-8s %4d" % (k, counts.get(k, 0)))

    for label in ("link", "create", "stage"):
        rows = [d for d in decided if d["decision"] == label]
        if not rows:
            continue
        print("\n  %s (%d):" % (label, len(rows)))
        for d in rows[:12]:
            print("    %-26s %-4s %-11s %s"
                  % (d["name"][:26], d["team_abbr"],
                     d["birthdate"] or "no dob", d.get("why", "")))
        if len(rows) > 12:
            print("    … and %d more" % (len(rows) - 12))


def cmd_build(con, season):
    con.execute(DDL)
    people = []
    print("  fetching %d rosters…" % len(TEAMS))
    for tid, abbr in TEAMS.items():
        payload = fetch_roster(tid, season, force=True)
        for r in rows_of(payload):
            people.append(parse_player(r, abbr))
    print("  %d players on rosters" % len(people))

    decided = resolve(con, people)
    today = datetime.now().date()

    creating = [d for d in decided if d["decision"] == "create"]
    ids = next_ids(con, len(creating))
    for d, pid in zip(creating, ids):
        d["player_id"] = pid
        con.execute("""
            INSERT INTO players (player_id, full_name, display_name,
                   name_normalized, birthdate, birthdate_status, status,
                   provenance, created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 'active', 'scraped',
                    current_timestamp, current_timestamp, 'ingest_rosters')""",
            [pid, d["name"], d["name"], d["name_norm"], d["birthdate"],
             "confirmed" if d["birthdate"] else "missing"])

    for d in decided:
        if d["decision"] in ("create", "link"):
            con.execute("""
                INSERT INTO player_identifiers (player_id, source, source_id)
                VALUES (?, 'nba', ?) ON CONFLICT DO NOTHING""",
                [d["player_id"], d["nba_id"]])
            # an approved match becomes an alias, so the next source that
            # names this player resolves without being asked again
            con.execute("""
                INSERT INTO player_aliases (player_id, alias, alias_normalized,
                       source, first_seen)
                VALUES (?, ?, ?, 'nba', current_timestamp)
                ON CONFLICT DO NOTHING""",
                [d["player_id"], d["name"], d["name_norm"]])

    staged = [d for d in decided if d["decision"] == "stage"]
    for d in staged:
        con.execute("""
            INSERT INTO staging_players (source, source_id, raw_name,
                   raw_name_norm, raw_birthdate, raw_team, raw_height_in,
                   raw_school, raw_league, first_seen, last_seen, occurrences,
                   match_status, proposed_player_id, match_score, match_reasons)
            VALUES ('nba', ?, ?, ?, ?, ?, ?, ?, 'NBA', current_timestamp,
                    current_timestamp, 1, 'pending', ?, ?, ?)
            ON CONFLICT DO NOTHING""",
            [d["nba_id"], d["name"], d["name_norm"], d["birthdate"],
             d["team_abbr"], d["height_in"], d["school"],
             d["player_id"], d.get("score"), d.get("why")])

    placed = [d for d in decided if d["player_id"] and d["decision"] != "stage"]
    con.executemany("""
        INSERT INTO team_rosters (as_of, season, team_abbr, player_id, jersey,
               position_raw, height_in, weight_lb, experience, school, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nba')
        ON CONFLICT (as_of, team_abbr, player_id) DO NOTHING""",
        [(today, season, d["team_abbr"], d["player_id"], d["jersey"],
          d["position_raw"], d["height_in"], d["weight_lb"], d["experience"],
          d["school"]) for d in placed])

    con.commit()
    print("\n  created  %d new players" % len(creating))
    print("  linked   %d to existing registry entries" % sum(
        1 for d in decided if d["decision"] == "link"))
    print("  staged   %d for review" % len(staged))
    print("  roster   %d rows for %s" % (len(placed), today))
    if staged:
        print("\n  Review the staged ones — they are names that matched something")
        print("  but not well enough to decide automatically:")
        print("    select raw_name, raw_team, proposed_player_id, match_score,")
        print("           match_reasons from staging_players where match_status='pending';")


def cmd_roster(con, team):
    rows = con.execute("""
        SELECT r.jersey, p.full_name, r.position_raw, r.height_in, r.weight_lb,
               r.experience, r.school
        FROM team_rosters r JOIN players p ON p.player_id = r.player_id
        WHERE r.team_abbr = ?
          AND r.as_of = (SELECT MAX(as_of) FROM team_rosters)
        ORDER BY CAST(NULLIF(regexp_replace(r.jersey,'[^0-9]','','g'),'') AS INT)""",
        [team]).fetchall()
    if not rows:
        print("  nothing for %s — run build first" % team)
        return
    print("  %s, %d players\n" % (team, len(rows)))
    for j, name, pos, h, w, exp, school in rows:
        print("  %-4s %-26s %-6s %-6s %-5s %-3s %s"
              % (j or "", name[:26], pos or "", 
                 "%d-%d" % (h // 12, h % 12) if h else "",
                 w or "", exp or "", school or ""))


if __name__ == "__main__":
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "roster"])
    ap.add_argument("--season", default=SEASON)
    ap.add_argument("--team")
    a = ap.parse_args()
    con = duckdb.connect(DB_PATH, read_only=(a.cmd == "check"))
    if a.cmd == "check":
        cmd_check(con, a.season)
    elif a.cmd == "build":
        cmd_build(con, a.season)
    else:
        cmd_roster(con, a.team or "BOS")
    con.close()
