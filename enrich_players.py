#!/usr/bin/env python3
"""
enrich_players.py — create the missing players, then fill in who they are.

977 people have appeared in an NBA preseason game since 2003 without ever
playing a regular-season minute, so Basketball Reference never made them a page
and the registry has never heard of them. They are the camp invites, the
G-League careers, the Europeans who got a look — exactly the population a
scouting database is supposed to know about.

Two steps, deliberately separate:

  add       create a registry row from what the game log already gives: a name
            and an NBA person id. Thin, but real — a player row is a place to
            put what you learn, not a claim to already know it.

  enrich    commonplayerinfo takes that person id and returns birthdate,
            height, weight, school, country and draft position. One request per
            player, no name matching, no ambiguity. This is why the stable id
            matters: the alternative is searching the web for "Brady Heslip" and
            hoping it is the right one.

Anyone the registry already has is enriched too if their record is thin, so a
player carried over from Basketball Reference without a birthdate gets one.

    python3 enrich_players.py add --dry-run
    python3 enrich_players.py add
    python3 enrich_players.py enrich --limit 50      # try a few first
    python3 enrich_players.py enrich
    python3 enrich_players.py status
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
CACHE = os.path.join(HOME, "raw", "playerinfo")
sys.path.insert(0, HOME)

HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json, text/plain, */*",
}
DELAY = 1.2


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("ø", "o"), ("ð", "d"), ("þ", "th"), ("æ", "ae"),
                 ("œ", "oe"), ("ł", "l"), ("đ", "d"), ("ß", "ss")):
        s = s.replace(a, b)
    s = s.replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    return " ".join(s.split())


def fetch_info(person_id, force=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s.json" % person_id)
    if os.path.exists(path) and not force:
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    from curl_cffi import requests as cr
    url = ("https://stats.nba.com/stats/commonplayerinfo?LeagueID=&PlayerID=%s"
           % person_id)
    for attempt in range(3):
        try:
            r = cr.get(url, headers=HEADERS, impersonate="chrome", timeout=45)
        except Exception:
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code != 200:
            time.sleep(10 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except Exception:
            time.sleep(8)
            continue
        json.dump(payload, open(path, "w", encoding="utf-8"))
        time.sleep(DELAY)
        return payload
    return None


def parse_info(payload):
    sets = (payload or {}).get("resultSets") or []
    if not sets or not sets[0].get("rowSet"):
        return None
    d = dict(zip(sets[0]["headers"], sets[0]["rowSet"][0]))

    birth = None
    if d.get("BIRTHDATE"):
        try:
            birth = datetime.strptime(str(d["BIRTHDATE"])[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    height = None
    h = str(d.get("HEIGHT") or "")
    if "-" in h:
        try:
            ft, inch = h.split("-")
            height = int(ft) * 12 + int(inch)
        except ValueError:
            pass

    draft_year = d.get("DRAFT_YEAR")
    draft_year = int(draft_year) if str(draft_year).isdigit() else None
    rnd = d.get("DRAFT_ROUND")
    pick = d.get("DRAFT_NUMBER")

    return {
        "full_name": d.get("DISPLAY_FIRST_LAST") or None,
        "first_name": d.get("FIRST_NAME") or None,
        "last_name": d.get("LAST_NAME") or None,
        "birthdate": birth,
        "height_in": height,
        "weight_lb": int(d["WEIGHT"]) if str(d.get("WEIGHT") or "").isdigit() else None,
        # the league writes the last school attended, which for an international
        # player is usually their country rather than a college
        "college": d.get("SCHOOL") or None,
        "country": d.get("COUNTRY") or None,
        "draft_year": draft_year,
        "draft_round": int(rnd) if str(rnd).isdigit() else None,
        "draft_pick": int(pick) if str(pick).isdigit() else None,
        "from_year": d.get("FROM_YEAR"),
        "to_year": d.get("TO_YEAR"),
    }


def missing_people(con):
    """Preseason appearances whose NBA id the registry has never linked."""
    from ingest_preseason import fetch_gamelog, parse_rows, build, bridge, SEASON_TYPE
    from ingest_preseason import season_list
    found = {}
    for s in season_list(con):
        try:
            rows = parse_rows(fetch_gamelog(s, SEASON_TYPE))
        except Exception:
            continue
        if not rows:
            continue
        _, box = build(rows)
        known, missing = bridge(con, box)
        for b in box:
            pid = b["nba_person_id"]
            if pid in missing and pid not in found:
                found[pid] = b["player_name"]
    return found


def next_ids(con, n):
    top = con.execute(
        "SELECT MAX(CAST(SUBSTR(player_id,2) AS INTEGER)) FROM players").fetchone()[0] or 0
    return ["P%08d" % (top + i + 1) for i in range(n)]


def cmd_add(con, dry):
    people = missing_people(con)
    print("  %d people to add" % len(people))
    if not people:
        return
    for pid, name in list(people.items())[:8]:
        print("    %-26s nba %s" % (name[:26], pid))
    if len(people) > 8:
        print("    … and %d more" % (len(people) - 8))
    if dry:
        print("\n  dry run — nothing written")
        return

    ids = next_ids(con, len(people))
    for (nba_id, name), our_id in zip(people.items(), ids):
        con.execute("""
            INSERT INTO players (player_id, full_name, display_name,
                   name_normalized, birthdate_status, status, provenance,
                   created_at, updated_at, created_by)
            VALUES (?, ?, ?, ?, 'missing', 'inactive', 'scraped',
                    current_timestamp, current_timestamp, 'enrich_players')""",
            [our_id, name, name, norm(name)])
        con.execute("""
            INSERT INTO player_identifiers (player_id, source, source_id)
            VALUES (?, 'nba', ?) ON CONFLICT DO NOTHING""", [our_id, nba_id])
        con.execute("""
            INSERT INTO player_aliases (player_id, alias, alias_normalized,
                   source, first_seen)
            VALUES (?, ?, ?, 'nba', current_timestamp) ON CONFLICT DO NOTHING""",
            [our_id, name, norm(name)])
    con.commit()
    print("\n  %d players created, each with their NBA id and an alias" % len(people))
    print("  next: python3 enrich_players.py enrich")


BIO_DDL = """
CREATE TABLE IF NOT EXISTS player_bio (
    player_id    VARCHAR NOT NULL,
    source       VARCHAR NOT NULL,
    birthdate    DATE,
    height_in    SMALLINT,
    weight_lb    SMALLINT,
    college      VARCHAR,
    country      VARCHAR,
    draft_year   SMALLINT,
    draft_round  SMALLINT,
    draft_pick   SMALLINT,
    from_year    VARCHAR,
    to_year      VARCHAR,
    fetched_at   TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, source)
)
"""


def thin_players(con, limit=None):
    """Anyone with an NBA id whose record is missing something worth having."""
    con.execute(BIO_DDL)
    q = """
        SELECT p.player_id, p.full_name, i.source_id
        FROM players p
        JOIN player_identifiers i
          ON i.player_id = p.player_id AND i.source = 'nba'
        LEFT JOIN player_bio b
          ON b.player_id = p.player_id AND b.source = 'nba'
        WHERE b.player_id IS NULL
          AND (p.birthdate IS NULL OR p.college IS NULL OR p.draft_year IS NULL)
        ORDER BY p.player_id"""
    if limit:
        q += " LIMIT %d" % limit
    return con.execute(q).fetchall()


def cmd_enrich(con, limit):
    # Written to its own table rather than into players. DuckDB refuses to
    # update a row that another table references by foreign key, and players is
    # referenced from half a dozen places — but keeping it separate is the
    # better shape regardless: the registry says who someone is, this says what
    # a particular source claimed about them, and a later source can disagree
    # without destroying the earlier answer.
    con.execute(BIO_DDL)
    todo = thin_players(con, limit)
    print("  %d players missing a birthdate, college or draft year" % len(todo))
    if not todo:
        return
    print("  about %.0f minutes\n" % (len(todo) * DELAY / 60))

    filled = {"birthdate": 0, "height_in": 0, "weight_lb": 0,
              "college": 0, "nationality": 0, "draft": 0}
    done = failed = 0
    for i, (our_id, name, nba_id) in enumerate(todo, 1):
        info = parse_info(fetch_info(nba_id))
        if not info:
            failed += 1
            continue
        con.execute("""
            INSERT INTO player_bio (player_id, source, birthdate, height_in,
                   weight_lb, college, country, draft_year, draft_round,
                   draft_pick, from_year, to_year)
            VALUES (?, 'nba', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (player_id, source) DO UPDATE SET
              birthdate = excluded.birthdate, height_in = excluded.height_in,
              weight_lb = excluded.weight_lb, college = excluded.college,
              country = excluded.country, draft_year = excluded.draft_year,
              draft_round = excluded.draft_round, draft_pick = excluded.draft_pick,
              from_year = excluded.from_year, to_year = excluded.to_year,
              fetched_at = now()""",
            [our_id, info["birthdate"], info["height_in"], info["weight_lb"],
             info["college"], info["country"], info["draft_year"],
             info["draft_round"], info["draft_pick"],
             str(info["from_year"] or "") or None, str(info["to_year"] or "") or None])

        for k in ("birthdate", "height_in", "weight_lb"):
            if info.get(k) is not None:
                filled[k] += 1
        if info.get("college"):
            filled["college"] += 1
        if info.get("country"):
            filled["nationality"] += 1
        if info.get("draft_year"):
            filled["draft"] += 1
        done += 1
        if i % 50 == 0:
            con.commit()
            print("    %d/%d" % (i, len(todo)))
    con.commit()
    print("\n  %d enriched, %d had no record at the league" % (done, failed))
    for k, v in filled.items():
        print("    %-12s %d" % (k, v))


def has_bio(con):
    """status runs read-only, so it cannot create the table it reads."""
    try:
        con.execute("SELECT 1 FROM player_bio LIMIT 1")
        return True
    except Exception:
        return False


def cmd_status(con):
    # coverage counted across both tables, since a field is known if either the
    # registry or an enrichment source has it
    r = con.execute("""
        SELECT COUNT(*) total,
               COUNT(COALESCE(p.birthdate, b.birthdate)) dob,
               COUNT(COALESCE(p.college, b.college)) school,
               COUNT(COALESCE(p.draft_year, b.draft_year)) draft,
               COUNT(b.height_in) height,
               SUM(CASE WHEN p.provenance = 'scraped' THEN 1 ELSE 0 END) scraped
        FROM players p
        LEFT JOIN player_bio b ON b.player_id = p.player_id AND b.source = 'nba'
    """).fetchone() if has_bio(con) else con.execute("""
        SELECT COUNT(*), COUNT(birthdate), COUNT(college), COUNT(draft_year),
               0, SUM(CASE WHEN provenance = 'scraped' THEN 1 ELSE 0 END)
        FROM players""").fetchone()
    print("  registry: %d players" % r[0])
    print("    birthdate   %5d  (%.0f%%)" % (r[1], 100 * r[1] / r[0]))
    print("    school      %5d  (%.0f%%)" % (r[2], 100 * r[2] / r[0]))
    print("    draft year  %5d  (%.0f%%)" % (r[3], 100 * r[3] / r[0]))
    print("    height      %5d  (%.0f%%)" % (r[4], 100 * r[4] / r[0]))
    print("    added by scrape rather than Basketball Reference: %d" % (r[5] or 0))
    n = con.execute("SELECT COUNT(*) FROM player_bio").fetchone()[0] if has_bio(con) else 0
    print("\n  %d enrichment records" % n)


if __name__ == "__main__":
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["add", "enrich", "status"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    con = duckdb.connect(DB_PATH, read_only=(a.cmd == "status" or a.dry_run))
    if a.cmd == "add":
        cmd_add(con, a.dry_run)
    elif a.cmd == "enrich":
        cmd_enrich(con, a.limit)
    else:
        cmd_status(con)
    con.close()
