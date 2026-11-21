#!/usr/bin/env python3
"""
compact_sync.py — shrink the working database and put it back on R2.

DuckDB doesn't give back the space of deleted rows. Replacing rows a day at a
time (the injury load did that 1,473 times) leaves the file far bigger than
its contents, and every nightly run pulls and pushes the whole file.

Under the lock: pull the current database, rewrite it with compact_db2.py
(which checks every table's row count before swapping), push it, release.

    python3 compact_sync.py
"""
import os
import subprocess
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import nightly2 as N

env = N.load_env()
s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
log = N.Log()
if not N.acquire_lock(s3, bucket, log):
    sys.exit("  another run holds the lock — try again when it finishes")
try:
    if not N.pull_db(s3, bucket, log):
        sys.exit("  could not pull the database")
    before = os.path.getsize(N.DB_PATH)
    if subprocess.call([sys.executable, "compact_db2.py"]) != 0:
        sys.exit("  compaction failed — nothing pushed; the database on R2 is unchanged")
    after = os.path.getsize(N.DB_PATH)
    print("  %.0f MB -> %.0f MB" % (before / 1048576.0, after / 1048576.0))
    N.push_db(s3, bucket, log)
    print("  pushed")
finally:
    N.release_lock(s3, bucket)
