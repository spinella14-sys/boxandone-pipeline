#!/usr/bin/env python3
"""
patch_rosters_online.py — run once. Rosters move off the laptop.

  nightly2.py         every nightly runs ingest_rosters_bbref.py (GitHub can
                      reach Basketball Reference), right after the NBA roster
                      step, which GitHub skips as before
  hourly.py           the hourly run refreshes rosters from Basketball
                      Reference when the transactions page changed, or when
                      there is no snapshot yet today — rosters only move
                      through transactions, so this keeps them current within
                      the hour without 30 requests every hour
  ingest_rosters.py   the NBA roster feed (laptop only) keeps its identity
                      work — NBA ids, birthdates, new players — but no longer
                      writes the daily snapshot, so the two sources never
                      overwrite each other's day
"""
import os
import sys

HOME = os.path.expanduser("~/boxandone")


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %-18s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected near:\n%s\n  nothing written to it" % (name, old[:100]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %-18s patched" % name)


patch("nightly2.py", [
    ('''        ok &= run(log, ["ingest_rosters.py", "build", "--season", season])
        # new players must exist in Supabase before a scout can tag them''',
     '''        ok &= run(log, ["ingest_rosters.py", "build", "--season", season])
        # the daily roster snapshot, from Basketball Reference: runs on GitHub
        ok &= run(log, ["ingest_rosters_bbref.py", "build"])
        # new players must exist in Supabase before a scout can tag them'''),
], "ingest_rosters_bbref.py")

patch("hourly.py", [
    ('''def main():
    if not laptop_gate():''',
     '''def tx_hash(season):
    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    try:
        rows = con.execute("SELECT transaction_id, raw_text FROM transactions WHERE season = ? "
                           "ORDER BY 1", [season]).fetchall()
    except Exception:
        rows = []
    con.close()
    return hashlib.sha1(repr(rows).encode()).hexdigest()


def roster_today():
    """Is there a Basketball Reference snapshot for today (Eastern) yet?"""
    import duckdb
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date()
    con = duckdb.connect(N.DB_PATH, read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM team_rosters WHERE as_of = ? AND source = 'bbref'",
                        [today]).fetchone()[0]
    except Exception:
        n = 0
    con.close()
    return n > 0


def main():
    if not laptop_gate():'''),
    ('''        ok &= N.run(log, ["ingest_transactions.py", "build", "--season", season, "--force"])
        ok &= N.run(log, ["ingest_rosters.py", "build", "--season", season])''',
     '''        tx_before = tx_hash(season)
        ok &= N.run(log, ["ingest_transactions.py", "build", "--season", season, "--force"])
        ok &= N.run(log, ["ingest_rosters.py", "build", "--season", season])
        # rosters only move through transactions: refresh them when the
        # transactions page changed, or when today has no snapshot yet
        if tx_hash(season) != tx_before or not roster_today():
            ok &= N.run(log, ["ingest_rosters_bbref.py", "build"])'''),
], "roster_today")

patch("ingest_rosters.py", [
    ('''    # replace today's snapshot: run hourly, a mid-day trade must not leave
    # the player listed on both teams for the day
    con.execute("DELETE FROM team_rosters WHERE as_of = ?", [today])
    con.executemany("""
        INSERT INTO team_rosters (as_of, season, team_abbr, player_id, jersey,
               position_raw, height_in, weight_lb, experience, school, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nba')
        ON CONFLICT (as_of, team_abbr, player_id) DO NOTHING""",
        [(today, season, d["team_abbr"], d["player_id"], d["jersey"],
          d["position_raw"], d["height_in"], d["weight_lb"], d["experience"],
          d["school"]) for d in placed])
''',
     '''    # The daily snapshot now comes from ingest_rosters_bbref.py, which runs
    # online every night; this feed (laptop only) keeps the identity work above
    # and no longer writes team_rosters, so the two never overwrite one day.
'''),
], "ingest_rosters_bbref.py")

print("\n  then: python3 ingest_rosters_bbref.py check")
