#!/usr/bin/env python3
"""
ingest_rosters_bbref.py — today's NBA rosters from Basketball Reference.

NBA.com's roster feed only answers home connections, so the daily roster
snapshot depended on the laptop. Basketball Reference publishes every team's
roster and answers GitHub, so the snapshot now comes from here and runs online.

Each roster row carries the player's Basketball Reference id, which resolves
straight to our player id. A player we do not hold by that id yet (a rookie,
a new signing) is matched by name and birthdate to a player we already have;
failing that he is listed as unmatched and left out of today's snapshot
rather than guessed or duplicated.

Writes today's rows to team_rosters (source 'bbref'), replacing any rows for
today, so a mid-day trade never leaves a player on two teams.

    python3 ingest_rosters_bbref.py check     # fetch and report, write nothing
    python3 ingest_rosters_bbref.py build
"""
import argparse
import gzip
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "raw", "rosters_bbref")
BASE = "https://www.basketball-reference.com"
DELAY = 4.0
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Basketball Reference team codes for the 30 current franchises
TEAMS = ["ATL", "BOS", "BRK", "CHO", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
         "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
         "OKC", "ORL", "PHI", "PHO", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]

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
)"""


def current_season():
    d = date.today()
    y = d.year if d.month >= 10 else d.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() or c == " " else (" " if c in "-–" else "") for c in s)
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


def inches(v):
    m = re.match(r"^\s*(\d+)-(\d+)\s*$", str(v or ""))
    return int(m.group(1)) * 12 + int(m.group(2)) if m else None


def fetch(team, end_year):
    import requests
    url = "%s/teams/%s/%d.html?_=%d" % (BASE, team, end_year, int(time.time()))
    for attempt in range(4):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
        except Exception as e:
            print("      %s: %s" % (team, str(e)[:60]))
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 200:
            r.encoding = "utf-8"
            os.makedirs(CACHE, exist_ok=True)
            with gzip.open(os.path.join(CACHE, "%s_%d.html.gz" % (team, end_year)), "wt",
                           encoding="utf-8") as f:
                f.write(r.text)
            return r.text
        if r.status_code == 429:
            print("      %s: rate limited, waiting" % team)
            time.sleep(60 * (attempt + 1))
            continue
        print("      %s: HTTP %s" % (team, r.status_code))
        time.sleep(10)
    return None


def parse(html, team):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="roster")
    if not table:
        return []
    out = []
    for tr in (table.find("tbody") or table).find_all("tr"):
        cell = lambda stat: tr.find(attrs={"data-stat": stat})
        p = cell("player")
        a = p.find("a") if p else None
        m = re.search(r"/players/\w/(\w+)\.html", a["href"]) if a and a.get("href") else None
        if not m:
            continue
        bd = cell("birth_date")
        birth = None
        if bd and bd.get("csk"):
            try:
                birth = datetime.strptime(bd["csk"][:8], "%Y%m%d").date()
            except ValueError:
                pass
        txt = lambda stat: (cell(stat).get_text(strip=True) if cell(stat) else "") or None
        out.append({
            "bbref_id": m.group(1), "name": a.get_text(strip=True), "team": team,
            "jersey": txt("number"), "pos": txt("pos"), "height_in": inches(txt("height")),
            "weight_lb": int(txt("weight")) if (txt("weight") or "").isdigit() else None,
            "birthdate": birth, "exp": txt("years_experience"), "school": txt("college"),
            "two_way": "(TW)" in tr.get_text(" "),
        })
    return out


def resolve(con, people):
    linked = dict(con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='bbref'").fetchall())
    has_bbref = set(linked.values())
    by_name = {}
    for pid, name, bd in con.execute("SELECT player_id, full_name, birthdate FROM players").fetchall():
        if pid not in has_bbref:
            by_name.setdefault(fold(name), []).append((pid, bd))
    for pid, alias in con.execute("SELECT player_id, alias FROM player_aliases").fetchall():
        if pid not in has_bbref:
            by_name.setdefault(fold(alias), []).append((pid, None))
    for p in people:
        if p["bbref_id"] in linked:
            p["decision"], p["player_id"] = "already", linked[p["bbref_id"]]
            continue
        # one entry per player; an alias row carries no birthdate, so it must
        # not overwrite the birthdate his player row brought
        cands = {}
        for pid, bd in by_name.get(fold(p["name"]), []):
            if cands.get(pid) is None:
                cands[pid] = bd
        exact = [pid for pid, bd in cands.items() if bd and p["birthdate"] and bd == p["birthdate"]]
        if len(exact) == 1:
            p["decision"], p["player_id"] = "link", exact[0]
        elif len(cands) == 1 and not p["birthdate"]:
            p["decision"], p["player_id"] = "link", next(iter(cands))
        else:
            # nobody by that name, several possible people, or a birthdate that
            # disagrees: not guessed and never created here, since a spelling
            # difference would make a second copy of a player we already hold.
            # He joins the snapshot once his id is linked (his first box score,
            # or the NBA roster feed when the laptop runs).
            p["decision"], p["player_id"] = "unmatched", None
    return people


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    a = ap.parse_args()
    import duckdb
    season = current_season()
    end_year = int(season[:4]) + 1
    people = []
    print("  fetching %d rosters from Basketball Reference…" % len(TEAMS))
    for i, t in enumerate(TEAMS):
        html = fetch(t, end_year)
        if html:
            people += parse(html, t)
        else:
            print("      %s: no roster — kept yesterday's for this team" % t)
        if i < len(TEAMS) - 1:
            time.sleep(DELAY + random.uniform(0, 1.5))
    teams_seen = {p["team"] for p in people}
    print("  %d players on %d rosters" % (len(people), len(teams_seen)))
    if len(teams_seen) < 25:
        sys.exit("  fewer than 25 rosters came back — nothing written")

    con = duckdb.connect(DB_PATH, read_only=(a.cmd == "check"))
    people = resolve(con, people)
    counts = {}
    for p in people:
        counts[p["decision"]] = counts.get(p["decision"], 0) + 1
    print("  " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    for p in people:
        if p["decision"] in ("link", "unmatched"):
            print("    %-6s %-26s %s %s" % (p["decision"], p["name"][:26], p["team"],
                                          p["birthdate"] or "no dob"))
    if a.cmd == "check":
        return

    con.execute(DDL)
    for p in people:
        if p["decision"] == "link":
            con.execute("""INSERT INTO player_identifiers
                             (source, source_id, player_id, source_url, is_primary, linked_by, provenance)
                           VALUES ('bbref', ?, ?, ?, TRUE, 'ingest_rosters_bbref', 'scraped')
                           ON CONFLICT DO NOTHING""",
                        [p["bbref_id"], p["player_id"],
                         "%s/players/%s/%s.html" % (BASE, p["bbref_id"][0], p["bbref_id"])])
    # the league's calendar day, not the server's: GitHub runs on UTC, which
    # rolls over at 8pm Eastern
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('America/New_York')).date()
    placed = [p for p in people if p["player_id"]]
    # replace today's rows for the teams that came back; a team whose page
    # failed keeps whatever it had
    for t in teams_seen:
        con.execute("DELETE FROM team_rosters WHERE as_of = ? AND team_abbr = ?", [today, t])
    con.executemany("""INSERT INTO team_rosters (as_of, season, team_abbr, player_id, jersey,
                         position_raw, height_in, weight_lb, experience, school, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'bbref')
                       ON CONFLICT (as_of, team_abbr, player_id) DO NOTHING""",
                    [(today, season, p["team"], p["player_id"], p["jersey"], p["pos"],
                      p["height_in"], p["weight_lb"], p["exp"], p["school"]) for p in placed])
    con.commit()
    con.close()
    print("  roster   %d rows for %s (%d two-way)" % (len(placed), today,
                                                     sum(1 for p in placed if p["two_way"])))


if __name__ == "__main__":
    main()
