#!/usr/bin/env python3
"""
sync_positions_now.py — bring the Positions tool's rulings into the registry
now, instead of waiting for the next hourly run.

Takes the database lock, pulls the current database from R2, copies the
latest ruling per player from Supabase into it, pushes it back and rebuilds
the registry, so search and the stats tables show the new positions.

    python3 sync_positions_now.py
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402
import positions_supabase as P  # noqa: E402

env = P.env_ok()
s3 = N.s3c(env)
bucket = env["R2_BUCKET_NAME"]
log = N.Log()
if not N.acquire_lock(s3, bucket, log):
    sys.exit("  the nightly or hourly job holds the lock — try again in a few minutes")
try:
    if not N.pull_db(s3, bucket, log):
        sys.exit("  could not pull the database")
    P.cmd_pull(env)
    N.push_db(s3, bucket, log)
    N.rebuild_registry_and_manifest(env, log)
    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    for row in con.execute("""SELECT position, COUNT(*) FROM player_positions
                              WHERE source = 'admin' GROUP BY 1 ORDER BY 1""").fetchall():
        print("  %-3s %d" % row)
    con.close()
finally:
    N.release_lock(s3, bucket)
