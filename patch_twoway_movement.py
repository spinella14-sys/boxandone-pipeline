#!/usr/bin/env python3
"""
patch_twoway_movement.py — run once.

Adds NBA.com's player-movement feed (the data behind nba.com/players/transactions)
to ingest_twoway.py. Every row carries the NBA.com person id, so a two-way
signing or conversion is tied to the right player without matching names.

  nba_movement   every row of the feed, kept and classified:
                 twoway_sign, sign, convert_to_twoway, convert_to_standard,
                 waive, claim, trade
  signing dates  the tracker now takes a two-way player's signing date (and so
                 his prorated limit) from this feed first, Basketball Reference
                 second, the first day he appeared on the list last.

On GitHub the feed is fetched through the relay, which needs one line changed
to allow it (see the reply). On the Mac it is fetched directly.

    python3 patch_twoway_movement.py
    python3 ingest_twoway.py movement          # what it holds, nothing written
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "nba_movement" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()

PAIRS = [
    # the table
    ('''CREATE TABLE IF NOT EXISTS nba_active (''',
     '''CREATE TABLE IF NOT EXISTS nba_movement (
    txn_key VARCHAR PRIMARY KEY, txn_date DATE, season VARCHAR, kind VARCHAR,
    txn_type VARCHAR, description VARCHAR, person_id VARCHAR, player_id VARCHAR,
    team_id BIGINT, team_abbr VARCHAR, seen_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS nba_active ('''),

    # fetch + parse + store, before the tracker section
    ('''def season_bounds(con, season):''',
     '''MOVEMENT_PATH = "/js/data/playermovement/NBA_Player_Movement.json"
TEAM_IDS = {37: "ATL", 38: "BOS", 39: "CLE", 40: "NOP", 41: "CHI", 42: "DAL", 43: "DEN", 44: "GSW",
            45: "HOU", 46: "LAC", 47: "LAL", 48: "MIA", 49: "MIL", 50: "MIN", 51: "BKN", 52: "NYK",
            53: "ORL", 54: "IND", 55: "PHI", 56: "PHX", 57: "POR", 58: "SAC", 59: "SAS", 60: "OKC",
            61: "TOR", 62: "UTA", 63: "MEM", 64: "WAS", 65: "DET", 66: "CHA"}


def fetch_movement():
    """NBA.com's player-movement feed: through the relay on GitHub, directly elsewhere."""
    from curl_cffi import requests as cr
    relay = os.environ.get("NBA_RELAY_URL")
    if relay and os.environ.get("GITHUB_ACTIONS"):
        url = relay.rstrip("/") + MOVEMENT_PATH
        headers = {"x-relay-token": os.environ.get("NBA_RELAY_TOKEN", "")}
    else:
        url = "https://stats.nba.com" + MOVEMENT_PATH
        headers = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com"}
    for attempt in range(3):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
            if r.status_code == 200:
                return r.json()["NBA_Player_Movement"]["rows"]
            print("    player movement: HTTP %s" % r.status_code)
        except Exception as e:
            print("    player movement: %s" % str(e)[:70])
        time.sleep(5 * (attempt + 1))
    return None


def season_of(d):
    """July onward belongs to the season that starts that fall."""
    y = d.year if d.month >= 7 else d.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


def movement_kind(t, desc):
    two = "two-way" in desc.lower()
    if t == "Signing":
        return "twoway_sign" if two else "sign"
    if t == "ContractConverted":
        return "convert_to_twoway" if re.search(r"to a two-way", desc, re.I) else "convert_to_standard"
    return {"Waive": "waive", "AwardOnWaivers": "claim", "Trade": "trade"}.get(t, (t or "other").lower())


def parse_movement(rows):
    out = []
    for r in rows:
        try:
            d = datetime.strptime(str(r["TRANSACTION_DATE"])[:10], "%Y-%m-%d").date()
            pid = str(int(float(r.get("PLAYER_ID") or 0))) or None
            tid = int(float(r.get("TEAM_ID") or 0))
        except Exception:
            continue
        desc = (r.get("TRANSACTION_DESCRIPTION") or "").strip()
        key = hashlib.md5(("%s|%s|%s" % (d, pid, desc)).encode()).hexdigest()[:16]
        out.append({"txn_key": key, "txn_date": d, "season": season_of(d),
                    "kind": movement_kind(r.get("Transaction_Type"), desc), "txn_type": r.get("Transaction_Type"),
                    "description": desc, "person_id": None if pid == "0" else pid,
                    "team_id": tid or None, "team_abbr": TEAM_IDS.get(tid - 1610612700) if tid else None})
    return out


def cmd_movement(con, rows=None, write=True):
    rows = rows if rows is not None else fetch_movement()
    if not rows:
        print("  player movement: feed unavailable — nothing changed")
        return False
    parsed = parse_movement(rows)
    if not write:
        return parsed
    ids = dict(con.execute("SELECT source_id, player_id FROM player_identifiers WHERE source = 'nba'").fetchall())
    before = con.execute("SELECT COUNT(*) FROM nba_movement").fetchone()[0]
    for r in parsed:
        con.execute("""INSERT INTO nba_movement VALUES (?,?,?,?,?,?,?,?,?,?, now())
                       ON CONFLICT (txn_key) DO UPDATE SET player_id = COALESCE(excluded.player_id, nba_movement.player_id)""",
                    [r["txn_key"], r["txn_date"], r["season"], r["kind"], r["txn_type"], r["description"],
                     r["person_id"], ids.get(r["person_id"]), r["team_id"], r["team_abbr"]])
    con.commit()
    s = con.execute("""SELECT COUNT(*), COUNT(player_id), MIN(txn_date) FROM nba_movement""").fetchone()
    print("  player movement: %d rows (%d new) back to %s; %d matched to a player by NBA id" % (
        s[0], s[0] - before, s[2], s[1]))
    return True


def season_bounds(con, season):'''),

    # signing dates: the feed first, BBRef second
    ('''    for pid, d, team in rows:
        out[pid] = (d, BBREF.get(team, team))
    return out''',
     '''    for pid, d, team in rows:
        out[pid] = (d, BBREF.get(team, team), "Basketball Reference")
    # NBA.com's feed, by person id, wins where it has the signing
    try:
        rows = con.execute("""
            SELECT player_id, txn_date, team_abbr FROM nba_movement
            WHERE season = ? AND kind IN ('twoway_sign', 'convert_to_twoway') AND player_id IS NOT NULL
            ORDER BY txn_date""", [season]).fetchall()
    except Exception:
        rows = []
    for pid, d, team in rows:
        out[pid] = (d, team, "NBA.com")
    return out'''),
    ('''        sign_date, sign_src = (s[0], "transaction") if s and (not abbr or s[1] == abbr) else (None, None)''',
     '''        sign_date, sign_src = (s[0], s[2]) if s and (not abbr or s[1] == abbr) else (None, None)'''),

    # run order and commands
    ('''        cmd_transactions(con, season)
        cmd_list(con, season)''',
     '''        cmd_transactions(con, season)
        cmd_movement(con)
        cmd_list(con, season)'''),
    ('''    ap.add_argument("cmd", choices=["run", "sample"])''',
     '''    ap.add_argument("cmd", choices=["run", "sample", "movement"])'''),
    ('''    if a.cmd == "sample":
        cmd_sample()
        return''',
     '''    if a.cmd == "sample":
        cmd_sample()
        return
    if a.cmd == "movement":
        from collections import Counter
        rows = cmd_movement(None, write=False) or []
        season = a.season or current_season()
        this = [r for r in rows if r["season"] == season]
        print("  %d rows, %s to %s" % (len(rows), min(r["txn_date"] for r in rows), max(r["txn_date"] for r in rows)))
        print("  rows by season:", ", ".join("%s %d" % kv for kv in sorted(Counter(r["season"] for r in rows).items())))
        print("  %s by kind: %s" % (season, Counter(r["kind"] for r in this).most_common()))
        for r in [r for r in this if r["kind"] in ("twoway_sign", "convert_to_twoway", "convert_to_standard")][:8]:
            print("    %s %-20s %-4s %s" % (r["txn_date"], r["kind"], r["team_abbr"], r["description"][:90]))
        return'''),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  ingest_twoway.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  ingest_twoway.py patched: NBA.com player movement, signing dates by NBA id")
