#!/usr/bin/env python3
"""
positions_supabase.py — admin positions live in Supabase; this moves them.

The site's Positions tool (/admin/positions) writes to Supabase's
player_positions table (one row per player per date, admin-only writes).
That is the record of every ruling. The pipeline reads it from there:

    python3 positions_supabase.py check   # the table's shape, row count, your scout id
    python3 positions_supabase.py from-reports [--write]   # positions given in HELP reports
    python3 positions_supabase.py seed    # put Adam's 49 labels (14 Oct) in, once
    python3 positions_supabase.py pull    # latest ruling per player -> DuckDB

`pull` writes DuckDB's player_positions with source 'admin', which the
registry export prefers over any automatic position. The hourly and nightly
jobs run it, so a ruling reaches player pages and search within the hour.
Uses SUPABASE_SERVICE_ROLE_KEY from ~/boxandone/.env.
"""
import os
import sys
import unicodedata

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402
import sync_players_supabase as SP  # noqa: E402

LABEL_DATE = "2026-10-14"
LABELS = [
    ('Nikola Jokić', 'C', 'spends too much time at the 5 in DEN throughout his career; very rarely shares the floor with another C. Weight profile clearly that of a C. Remember: positions are more about D than O'),
    ('Giannis Antetokounmpo', 'F', 'could make argument he was a W earlier in his career; size dimensions + ability to play at the 5 in spurts necessitates a switch to F'),
    ('Stephen Curry', 'PG', ''),
    ('Jayson Tatum', 'W', ''),
    ('Neemias Queta', 'C', ''),
    ('Victor Wembanyama', 'C', ''),
    ('Shai Gilgeous-Alexander', 'CG', ''),
    ('Luka Dončić', 'CG', 'defensive profile like that of a W; offensive like that of a PG. This splits difference'),
    ('Draymond Green', 'F', 'spends too much time playing the 5 in his career to be a wing'),
    ('Evan Mobley', 'F', "would understand argument he'd be a C, but most of his minutes come alongside a C, and he has more of a mid-range/ 3pt offensive profile"),
    ('Jaren Jackson Jr.', 'F', 'very similar rationale to Evan Mobley'),
    ('Bam Adebayo', 'C', 'spends too much at the 5 during his career'),
    ('Scottie Barnes', 'W', 'close to a F, but likely spends enough time handling + avoiding major minutes at the 5 to qualify as a W. Plus has no issues defending smaller wings'),
    ('Derrick White', 'CG', ''),
    ('Dyson Daniels', 'CG', ''),
    ('Herbert Jones', 'W', ''),
    ('Josh Hart', 'W', ''),
    ('Duncan Robinson', 'W', ''),
    ('Cam Thomas', 'CG', ''),
    ('LaMelo Ball', 'PG', 'size is that of a CG, but such an on-ball-heavy offensive profile, mainly with his facilitating tendencies, pushes him to PG'),
    ('Trae Young', 'PG', ''),
    ('Ausar Thompson', 'W', ''),
    ('Tim Duncan', 'C', 'started career as F'),
    ('Dirk Nowitzki', 'F', 'despite drop in footspeed late in career, Dirk needed defensive insulation, so most of his minutes came with another rim protector'),
    ('Kevin Garnett', 'C', ''),
    ('Rasheed Wallace', 'C', ''),
    ('Shane Battier', 'W', ''),
    ('Kyle Korver', 'W', ''),
    ('Chris Paul', 'PG', ''),
    ('Tony Allen', 'W', 'because CGs have some form of "they can play spot minutes at the point" equity, Allen\'s lack of offensive contributions or handling likely make him more a W'),
    ('Luguentz Dort', 'W', ''),
    ('Isaiah Stewart', 'C', 'primarily deployed as backup 5'),
    ('Paul Reed', 'C', 'primarily deployed as backup 5'),
    ('Isaiah Jackson', 'C', 'primarily deployed as backup 5'),
    ('Bobby Portis', 'F', "shooting profile + some defensive versatility allow him to play more viably next to another big. Argument could be made that he's close to switching to a C, and wouldn't be surprised if that happens this season in Miami"),
    ('Nikola Jović', 'F', 'struggles guarding smaller wing types, so not a W'),
    ('Jalen Smith', 'F', 'shooting profile + how much he plays next to another C optimally makes him a F, but he is very much a tweener between a F and a C'),
    ('Kobe Brown', 'F', ''),
    ('Carter Bryant', 'F', ''),
    ('Walter Clayton', 'PG', ''),
    ('Ryan Dunn', 'W', "defensive profile allows him to successfully defend smaller; doesn't have much viability to guard up against 5s"),
    ('Svi Mykhailiuk', 'W', 'too off-ball to be a CG'),
    ('Zeke Nnaji', 'F', 'very close between F and C, but been deployed next to primary rim protectors too much lately to view as a C'),
    ('Noah Penda', 'F', ''),
    ('Jalen Pickett', 'PG', 'very on-ball-heavy profile makes him more PG than CG'),
    ('Jordan Poole', 'CG', 'more balanced/ off-ball profile than Pickett, plus is more of a scorer who operates in off-ball or ISO than PNR-heavy usage'),
    ('Terrence Shannon Jr.', 'W', ''),
    ('Ben Sheppard', 'W', "very close between W and CG; view his role as so much off-ball offensively that he's more a W"),
    ('Julian Strawther', 'W', 'very off-ball shooter profile; not enough handling to get to CG'),
]


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def env_ok():
    env = N.load_env()
    if not env.get("SUPABASE_SERVICE_ROLE_KEY"):
        sys.exit("  SUPABASE_SERVICE_ROLE_KEY missing from ~/boxandone/.env")
    return env


def fetch_all(env):
    rows, off = [], 0
    while True:
        code, page = SP.call(env, None, "player_positions?select=player_id,position,effective_from,"
                             "assigned_by,reason&order=player_id,effective_from.desc"
                             "&limit=1000&offset=%d" % off)
        if code not in (200, 206):
            sys.exit("  could not read player_positions: HTTP %s %s" % (code, page))
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def cmd_check(env):
    code, sample = SP.call(env, None, "player_positions?select=*&limit=2")
    print("  player_positions: HTTP %s" % code)
    if code != 200:
        print("  %s" % sample)
        return
    print("  columns: %s" % (sorted(sample[0]) if sample else "(table empty, columns not shown)"))
    print("  rows: %d" % len(fetch_all(env)))
    code, admins = SP.call(env, None, "scouts?select=scout_id,display_name,role&role=eq.super_admin")
    print("  super_admin scouts: %s" % (admins if code == 200 else "HTTP %s" % code))


def cmd_seed(env):
    import duckdb
    code, admins = SP.call(env, None, "scouts?select=scout_id&role=eq.super_admin&active=eq.true")
    if code != 200 or len(admins) != 1:
        sys.exit("  need exactly one active super_admin scout to attribute the labels to: %s" % admins)
    who = admins[0]["scout_id"]
    con = duckdb.connect(N.DB_PATH, read_only=True)
    by = {}
    for pid, name in con.execute("SELECT player_id, full_name FROM players").fetchall():
        by.setdefault(fold(name), []).append(pid)
    con.close()
    rows, skipped = [], []
    for name, pos, note in LABELS:
        hits = by.get(fold(name), [])
        if len(hits) == 1:
            rows.append({"player_id": hits[0], "position": pos, "effective_from": LABEL_DATE,
                         "assigned_by": who, "reason": note or None})
        else:
            skipped.append("%s (%s)" % (name, "not found" if not hits else "%d players share the name" % len(hits)))
    code, body = SP.call(env, None, "player_positions?on_conflict=player_id,effective_from", "POST",
                         rows, prefer="resolution=merge-duplicates,return=minimal")
    if code not in (200, 201, 204):
        sys.exit("  write failed: HTTP %s %s" % (code, body))
    print("  stored %d rulings, attributed to %s" % (len(rows), who))
    for s in skipped:
        print("  SKIPPED %s — set it in the Positions tool" % s)


def cmd_pull(env):
    import duckdb
    latest = {}
    for r in fetch_all(env):                 # newest first within each player
        latest.setdefault(r["player_id"], r)
    con = duckdb.connect(N.DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS player_positions (
        player_id VARCHAR PRIMARY KEY, position VARCHAR NOT NULL, source VARCHAR NOT NULL,
        confidence DOUBLE, score DOUBLE, reasons VARCHAR, assigned_at TIMESTAMP DEFAULT now())""")
    con.execute("DELETE FROM player_positions WHERE source = 'admin'")
    for pid, r in latest.items():
        con.execute("DELETE FROM player_positions WHERE player_id = ?", [pid])
        con.execute("""INSERT INTO player_positions (player_id, position, source, confidence, reasons)
                       VALUES (?, ?, 'admin', 1.0, ?)""",
                    [pid, r["position"], "%s · %s%s" % (r["effective_from"], r["assigned_by"],
                                                         " · " + r["reason"] if r.get("reason") else "")])
    con.commit()
    con.close()
    print("  %d admin positions pulled from Supabase" % len(latest))


def fetch(env, path):
    rows, off = [], 0
    while True:
        code, page = SP.call(env, None, "%s&limit=1000&offset=%d" % (path, off))
        if code not in (200, 206):
            sys.exit("  could not read %s: HTTP %s %s" % (path.split("?")[0], code, page))
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def cmd_from_reports(env, write=False):
    """Positions already given in scouting reports, newest report per player.
    Written with that report's date, scout and id, so the history shows where
    each one came from. Adam's labels (seed) are dated later and win."""
    reps = fetch(env, "reports?select=report_id,player_id,position,report_date,scout_id,status"
                      "&position=not.is.null&order=player_id,report_date.desc")
    latest = {}
    for r in reps:
        if r.get("status") == "draft" or not r.get("player_id"):
            continue
        latest.setdefault(r["player_id"], r)
    print("  %d published reports carry a position, covering %d players" % (
        sum(1 for r in reps if r.get("status") != "draft"), len(latest)))

    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    names = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())
    con.close()
    labelled = {fold(n): p for n, p, _ in LABELS}
    agree = differ = 0
    for pid, r in sorted(latest.items(), key=lambda kv: names.get(kv[0], "")):
        mine = labelled.get(fold(names.get(pid, "")))
        flag = ""
        if mine:
            if mine == r["position"]:
                agree += 1
            else:
                differ += 1
                flag = "   <- your 14 Oct label says %s" % mine
        print("  %-26s %-3s %s · %s%s" % ((names.get(pid) or pid)[:26], r["position"],
                                          str(r["report_date"])[:10], r["scout_id"], flag))
    print("\n  against your 49 labels: %d agree, %d differ" % (agree, differ))
    if not write:
        print("  nothing written — add --write to store these")
        return
    rows = [{"player_id": pid, "position": r["position"],
             "effective_from": str(r["report_date"])[:10], "assigned_by": r["scout_id"],
             "report_id": r["report_id"], "reason": "from HELP report"}
            for pid, r in latest.items()]
    code, body = SP.call(env, None, "player_positions?on_conflict=player_id,effective_from", "POST",
                         rows, prefer="resolution=merge-duplicates,return=minimal")
    if code not in (200, 201, 204):
        sys.exit("  write failed: HTTP %s %s" % (code, body))
    print("  stored %d positions from reports" % len(rows))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    env = env_ok()
    if cmd == "from-reports":
        cmd_from_reports(env, "--write" in sys.argv)
    else:
        {"check": cmd_check, "seed": cmd_seed, "pull": cmd_pull}.get(
            cmd, lambda e: sys.exit("  use check, from-reports, seed or pull"))(env)
