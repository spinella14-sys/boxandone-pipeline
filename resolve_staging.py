#!/usr/bin/env python3
"""
resolve_staging.py — turn pending staging rows into real registry players.

Why this is needed: BBRef's alphabetical player index at /players/{letter}/ is
NOT a complete list of everyone who appears in a box score. Verified — 22
players in box scores are absent from all 26 index pages. Re-seeding will never
find them. So the staging queue is the permanent mechanism for these, not a
workaround, and this script is the tool that drains it.

Also fixes a bug it exposed: ingest_games inserted a fresh staging row per game
instead of incrementing the occurrences counter, so 22 players produced 64 rows.

Phases:
    python3 resolve_staging.py dedupe     # collapse duplicate staging rows
    python3 resolve_staging.py fetch      # pull each player's BBRef page (cached)
    python3 resolve_staging.py approve    # mint IDs, write registry rows
    python3 resolve_staging.py reparse    # queue affected games for re-parse

Nothing auto-creates a player without 'approve' being run explicitly.
"""

import argparse
import gzip
import os
import re
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PLAYER_DIR = os.path.join(HOME, "raw", "bbref_players")
BASE = "https://www.basketball-reference.com"
DELAY = 5.5

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}


def db():
    import duckdb
    return duckdb.connect(DB_PATH)


# ---------------------------------------------------------------------------

def cmd_dedupe():
    con = db()
    before = con.execute(
        "SELECT COUNT(*) FROM staging_players WHERE match_status='pending'").fetchone()[0]

    con.execute("""
        CREATE OR REPLACE TEMP TABLE keep AS
        SELECT MIN(staging_id) AS staging_id, source, source_id, COUNT(*) AS n
        FROM staging_players
        WHERE match_status='pending'
        GROUP BY source, source_id
    """)
    con.execute("""
        UPDATE staging_players SET occurrences = k.n
        FROM keep k WHERE staging_players.staging_id = k.staging_id
    """)
    con.execute("""
        DELETE FROM staging_players
        WHERE match_status='pending'
          AND staging_id NOT IN (SELECT staging_id FROM keep)
    """)
    con.commit()

    after = con.execute(
        "SELECT COUNT(*) FROM staging_players WHERE match_status='pending'").fetchone()[0]
    print(f"  {before} rows -> {after} distinct players")
    for r in con.execute("""SELECT source_id, raw_name, raw_team, occurrences
                            FROM staging_players WHERE match_status='pending'
                            ORDER BY occurrences DESC""").fetchall():
        print(f"    {r[0]:14}{r[1]:26}{str(r[2]):6}{r[3]:>4} games")
    con.close()


# ---------------------------------------------------------------------------

def parse_player_page(html):
    """Pull bio from a BBRef player page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    bd = soup.find(id="necro-birth")
    if bd and bd.get("data-birth"):
        out["birthdate"] = bd["data-birth"]

    meta = soup.find("div", id="meta")
    text = meta.get_text(" ", strip=True) if meta else ""

    m = re.search(r"(\d)-(\d{1,2})\s*,\s*(\d{2,3})lb", text)
    if m:
        out["height_in"] = int(m.group(1)) * 12 + int(m.group(2))
        out["weight_lb"] = int(m.group(3))

    if meta:
        for p in meta.find_all("p"):
            t = p.get_text(" ", strip=True)
            if t.startswith("College:"):
                out["college"] = t.split(":", 1)[1].strip()
            elif t.startswith("Position:"):
                out["bbref_pos"] = t.split(":", 1)[1].split("▪")[0].strip()

    h1 = soup.find("h1")
    if h1:
        out["full_name"] = h1.get_text(strip=True)
    return out


def cmd_fetch(force=False):
    import requests
    os.makedirs(PLAYER_DIR, exist_ok=True)
    con = db()
    rows = con.execute("""SELECT source_id, raw_name FROM staging_players
                          WHERE match_status='pending' ORDER BY source_id""").fetchall()
    con.close()
    if not rows:
        print("  nothing pending")
        return

    s = requests.Session()
    s.headers.update(UA)
    got = fail = 0
    for bid, name in rows:
        path = os.path.join(PLAYER_DIR, f"{bid}.html.gz")
        if os.path.exists(path) and not force:
            print(f"    {bid:14} cached")
            continue
        url = f"{BASE}/players/{bid[0]}/{bid}.html"
        try:
            r = s.get(url, timeout=60)
            r.encoding = "utf-8"
        except Exception as e:
            print(f"    {bid:14} error {str(e)[:40]}")
            fail += 1
            time.sleep(DELAY)
            continue
        if r.status_code == 200:
            gzip.open(path, "wt", encoding="utf-8").write(r.text)
            print(f"    {bid:14} ok   {name}")
            got += 1
        else:
            print(f"    {bid:14} HTTP {r.status_code}")
            fail += 1
        time.sleep(DELAY)
    print(f"\n  fetched {got}, failed {fail}")


def cmd_approve():
    sys.path.insert(0, HOME)
    from identity import normalize_name
    con = db()

    rows = con.execute("""SELECT staging_id, source_id, raw_name, raw_team, occurrences
                          FROM staging_players WHERE match_status='pending'
                          ORDER BY source_id""").fetchall()
    if not rows:
        print("  nothing pending")
        return

    seq = con.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(player_id,2) AS INTEGER)),0) FROM players"
    ).fetchone()[0]

    made = skipped = nobio = 0
    for sid, bid, name, team, occ in rows:
        exists = con.execute(
            "SELECT player_id FROM player_identifiers WHERE source='bbref' AND source_id=?",
            [bid]).fetchone()
        if exists:
            con.execute("UPDATE staging_players SET match_status='auto_linked', "
                        "proposed_player_id=?, resolved_at=now() WHERE staging_id=?",
                        [exists[0], sid])
            skipped += 1
            continue

        bio = {}
        path = os.path.join(PLAYER_DIR, f"{bid}.html.gz")
        if os.path.exists(path):
            try:
                bio = parse_player_page(gzip.open(path, "rt", encoding="utf-8").read())
            except Exception as e:
                print(f"    {bid}: parse failed {str(e)[:50]}")
        else:
            nobio += 1

        full = bio.get("full_name") or name
        n = normalize_name(full)
        seq += 1
        pid = f"P{seq:08d}"
        bd = bio.get("birthdate")

        con.execute("""
            INSERT INTO players (player_id, full_name, display_name, first_name,
              last_name, name_normalized, birthdate, birthdate_status, height_in,
              weight_lb, college, status, provenance, created_by, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'active','scraped','resolve_staging',?)
        """, [pid, full, full, n["first"], n["last"], n["name_key"], bd,
              "unconfirmed" if bd else "missing",
              bio.get("height_in"), bio.get("weight_lb"), bio.get("college"),
              f"not in bbref index; {occ} game(s); last team {team}"])

        con.execute("""INSERT INTO player_identifiers
              (source, source_id, player_id, source_url, is_primary, linked_by, provenance)
              VALUES ('bbref',?,?,?,TRUE,'resolve_staging','scraped')""",
                    [bid, pid, f"{BASE}/players/{bid[0]}/{bid}.html"])
        con.execute("""INSERT INTO player_aliases
              (player_id, alias, alias_normalized, source) VALUES (?,?,?,'bbref')""",
                    [pid, full, n["name_key"]])
        con.execute("UPDATE staging_players SET match_status='approved', "
                    "proposed_player_id=?, resolved_at=now(), resolved_by='admin' "
                    "WHERE staging_id=?", [pid, sid])
        print(f"    {pid}  {full:26} bday={bd or 'MISSING':10} {occ} game(s)")
        made += 1

    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    print(f"\n  created {made}, already linked {skipped}, no cached bio {nobio}")
    print(f"  registry now {tot:,}")
    con.close()


def cmd_reparse():
    """Mark games containing newly-approved players so their rows get written."""
    con = db()
    ids = [r[0] for r in con.execute(
        "SELECT source_id FROM staging_players WHERE match_status='approved'").fetchall()]
    if not ids:
        print("  nothing approved")
        return
    # any game whose box row count is short is cheapest to just re-parse wholesale
    n = con.execute("""
        UPDATE ingest_log SET parsed=FALSE
        WHERE game_id IN (
            SELECT g.game_id FROM games g
            JOIN player_game_box b ON b.game_id = g.game_id
            GROUP BY g.game_id
            HAVING COUNT(*) < 20
        )
    """)
    todo = con.execute("SELECT COUNT(*) FROM ingest_log WHERE fetched AND NOT parsed").fetchone()[0]
    con.commit()
    con.close()
    print(f"  {todo} games queued for re-parse")
    print("  next: python3 ingest_games.py parse")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dedupe", "fetch", "approve", "reparse"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    {"dedupe": cmd_dedupe,
     "fetch": lambda: cmd_fetch(a.force),
     "approve": cmd_approve,
     "reparse": cmd_reparse}[a.cmd]()
