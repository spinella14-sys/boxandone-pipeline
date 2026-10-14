#!/usr/bin/env python3
"""
assign_positions.py — a first-pass position for every player, for Adam to edit.

Positions are PG, CG, W, F, C (Box and One's own, not NBA positions). This is
a starting point, not a ruling: every assignment carries a confidence, and the
low-confidence ones are what the review queue puts first. An admin-set
position always wins and is never touched.

How a position is estimated, on a 1-5 line (1 = PG, 5 = C):

  size        height mapped onto the line: 6'1" = 1, each 2.5" another step,
              6'11"+ = 5. Weight nudges it: heavy for his height leans bigger.
  role        BPM 2.0's own position estimate (pos_est), which reads a
              player's share of his team's rebounds, assists, steals, blocks
              and fouls. Minute-weighted over his three most recent regular
              seasons with 300+ minutes.
  score       0.45 x size + 0.55 x role (role alone when height is missing)

Cut points on the score: PG < 1.9, CG < 2.7, W < 3.5, F < 4.3, C above.

Confidence falls when the score sits near a cut point, when height is
missing, when minutes are few, and when size and role disagree by more than
a full position (the small-ball centers and big wings Adam wants first).

    python3 assign_positions.py check     # writes positions_v1.csv, changes nothing
    python3 assign_positions.py apply     # stores them and rebuilds the registry

apply takes the database lock, pulls the current database from R2, writes the
player_positions table, pushes it back and rebuilds the registry, so it never
overwrites what the nightly or hourly job wrote in the meantime.
"""
import argparse
import csv
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

VERSION = "auto_v1"
W_SIZE, W_ROLE = 0.45, 0.55
CUTS = [("PG", 1.9), ("CG", 2.7), ("W", 3.5), ("F", 4.3), ("C", 99)]
CHECK_NAMES = ["Nikola Jok", "Giannis", "Stephen Curry", "Jayson Tatum", "Neemias Queta",
               "Victor Wemb", "Shai Gilgeous", "Luka Don", "Draymond Green", "Evan Mobley",
               "Jaren Jackson", "Bam Adebayo", "Scottie Barnes", "Derrick White",
               "Dyson Daniels", "Herbert Jones", "Josh Hart", "Duncan Robinson",
               "Cam Thomas", "LaMelo Ball", "Trae Young", "Ausar Thompson", "Tim Duncan",
               "Dirk Nowitzki", "Kevin Garnett", "Rasheed Wallace", "Shane Battier",
               "Kyle Korver", "Chris Paul", "Tony Allen"]


def size_pos(h, w):
    if not h:
        return None
    s = 1.0 + (h - 73) / 2.5
    if w:
        # a typical weight for this height, roughly; 15 lb heavy = a fifth of a step
        typical = 190 + (h - 73) * 6.5
        s += (w - typical) / 75.0
    return max(1.0, min(5.0, s))


def position_for(score):
    for code, cut in CUTS:
        if score < cut:
            return code
    return "C"


def margin(score):
    """Distance to the nearest cut point, in score units."""
    edges = [c for _, c in CUTS[:-1]]
    return min(abs(score - e) for e in edges)


def season_rows(s3, bucket, tmp, con):
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": "v2/seasons/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])
                 if o["Key"].endswith("/player_season.parquet")]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    def fetch(k):
        p = os.path.join(tmp, k.replace("/", "_"))
        s3.download_file(bucket, k, p)
        return p

    with ThreadPoolExecutor(max_workers=16) as pool:
        paths = list(pool.map(fetch, keys))
    cur = con.execute("""
        SELECT player_id, season, mp, pos_est, fga, fg3a, trb, ast, blk, ra_fga
        FROM read_parquet([%s], union_by_name=true)
        WHERE season_type = 'regular' AND mp > 0 AND pos_est IS NOT NULL
        ORDER BY player_id, season DESC""" % ",".join("'%s'" % p for p in paths))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r[0], []).append(r)
    return out


def estimate(players, seasons):
    rows = []
    for pid, name, h, w, admin in players:
        ss = seasons.get(pid, [])
        recent = [s for s in ss if s[2] >= 300][:3] or sorted(ss, key=lambda s: -s[2])[:3]
        mp = sum(s[2] for s in recent)
        role = (sum(s[3] * s[2] for s in recent) / mp) if mp else None
        size = size_pos(h, w)
        if role is None and size is None:
            continue
        if role is None:
            score, basis = size, "size only"
        elif size is None:
            score, basis = role, "stats only (no height)"
        else:
            score, basis = W_SIZE * size + W_ROLE * role, "size + role"
        pos = position_for(score)

        conf = min(1.0, margin(score) / 0.35)
        if size is None:
            conf *= 0.6
        if role is None or mp < 1000:
            conf *= 0.7
        conflict = size is not None and role is not None and abs(size - role) > 1.0
        if conflict:
            conf *= 0.7
        conf = round(max(0.05, conf), 2)

        fga = sum(s[4] or 0 for s in recent)
        why = []
        if h:
            why.append("%d-%d%s" % (h // 12, h % 12, " %d lb" % w if w else ""))
        if size is not None:
            why.append("size %.1f" % size)
        if role is not None:
            why.append("role %.1f over %s min (%s)" % (role, format(int(mp), ","),
                       ", ".join(s[1] for s in recent)))
        if fga:
            why.append("3PA %d%% / rim %d%% of FGA"
                       % (100 * sum(s[5] or 0 for s in recent) / fga,
                          100 * sum(s[9] or 0 for s in recent) / fga))
        if conflict:
            why.append("SIZE AND ROLE DISAGREE")
        rows.append({
            "player_id": pid, "full_name": name, "admin_position": admin,
            "position": pos, "score": round(score, 2),
            "size": round(size, 2) if size is not None else None,
            "role": round(role, 2) if role is not None else None,
            "minutes": int(mp), "last_season": recent[0][1] if recent else None,
            "confidence": conf, "basis": basis, "conflict": conflict,
            "reasons": " · ".join(why),
        })
    return rows


def load(env, s3):
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % N.DB_PATH)
    has_bio = con.execute("""SELECT COUNT(*) FROM duckdb_tables()
        WHERE database_name='hot' AND table_name='player_bio'""").fetchone()[0] > 0
    bio = ("LEFT JOIN hot.player_bio bio ON bio.player_id=p.player_id AND bio.source='nba'"
           if has_bio else "")
    hcol = "COALESCE(p.height_in, bio.height_in)" if has_bio else "p.height_in"
    wcol = "COALESCE(p.weight_lb, bio.weight_lb)" if has_bio else "p.weight_lb"
    players = con.execute("""
        SELECT p.player_id, p.full_name, %s, %s, p.position FROM hot.players p %s
        """ % (hcol, wcol, bio)).fetchall()
    with tempfile.TemporaryDirectory() as tmp:
        seasons = season_rows(s3, env["R2_BUCKET_NAME"], tmp, con)
    con.close()
    return players, seasons


def report(rows):
    active = [r for r in rows if r["last_season"] == "2025-26" and r["minutes"] >= 500]
    print("  %d players estimated, %d admin-set positions left untouched"
          % (len(rows), sum(1 for r in rows if r["admin_position"])))
    for label, pool in (("all players", rows), ("active (2025-26, 500+ min)", active)):
        n = len(pool) or 1
        print("\n  %s: %d" % (label, len(pool)))
        for code, _ in CUTS:
            k = sum(1 for r in pool if r["position"] == code)
            print("    %-3s %5d  %4.0f%%" % (code, k, 100 * k / n))
        lo = sum(1 for r in pool if r["confidence"] < 0.5)
        print("    low confidence (<0.5): %d   size/role disagree: %d"
              % (lo, sum(1 for r in pool if r["conflict"])))
    print("\n  named checks")
    for nm in CHECK_NAMES:
        hit = [r for r in rows if nm.lower() in (r["full_name"] or "").lower()]
        for r in hit[:1]:
            print("    %-24s %-3s conf %.2f  %s" % (r["full_name"][:24], r["position"],
                                                   r["confidence"], r["reasons"][:90]))
    print("\n  lowest confidence among active players")
    for r in sorted(active, key=lambda r: r["confidence"])[:20]:
        print("    %-24s %-3s conf %.2f  %s" % (r["full_name"][:24], r["position"],
                                               r["confidence"], r["reasons"][:90]))


def write_csv(rows):
    path = os.path.join(HOME, "positions_v1.csv")
    cols = ["full_name", "position", "confidence", "score", "size", "role", "minutes",
            "last_season", "basis", "reasons", "admin_position", "player_id"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["confidence"], r["full_name"] or "")):
            w.writerow(r)
    print("\n  every player, lowest confidence first: %s" % path)


def apply(env, s3, log):
    import duckdb
    bucket = env["R2_BUCKET_NAME"]
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  the nightly or hourly job holds the lock — try again in a few minutes")
    try:
        if not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
        players, seasons = load(env, s3)
        rows = estimate(players, seasons)
        con = duckdb.connect(N.DB_PATH)
        con.execute("""CREATE TABLE IF NOT EXISTS player_positions (
            player_id    VARCHAR PRIMARY KEY,
            position     VARCHAR NOT NULL,
            source       VARCHAR NOT NULL,     -- 'auto_v1', later 'admin'
            confidence   DOUBLE,
            score        DOUBLE,
            reasons      VARCHAR,
            assigned_at  TIMESTAMP DEFAULT now())""")
        # earlier automatic rows are replaced; anything an admin set stays
        con.execute("DELETE FROM player_positions WHERE source LIKE 'auto%'")
        kept = {r[0] for r in con.execute("SELECT player_id FROM player_positions").fetchall()}
        todo = [r for r in rows if not r["admin_position"] and r["player_id"] not in kept]
        con.executemany("""INSERT INTO player_positions
            (player_id, position, source, confidence, score, reasons) VALUES (?,?,?,?,?,?)""",
            [(r["player_id"], r["position"], VERSION, r["confidence"], r["score"], r["reasons"])
             for r in todo])
        con.commit()
        con.close()
        log("stored %d automatic positions (%d admin-set kept)" % (len(todo), len(kept)))
        N.push_db(s3, bucket, log)
        N.rebuild_registry_and_manifest(env, log)
    finally:
        N.release_lock(s3, bucket)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "apply"])
    a = ap.parse_args()
    env = N.load_env()
    s3 = N.s3c(env)
    if a.cmd == "check":
        players, seasons = load(env, s3)
        rows = estimate(players, seasons)
        report(rows)
        write_csv(rows)
        print("  check only — nothing written to the database")
    else:
        apply(env, s3, N.Log())


if __name__ == "__main__":
    main()
