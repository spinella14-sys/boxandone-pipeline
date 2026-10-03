#!/usr/bin/env python3
"""
compact_db.py — reclaim disk after archive_cold.py prune.

DuckDB does not release pages on DELETE. After pruning 29 seasons the file was
still 1.62 GB despite holding only 2025-26. The fix is a full rewrite:

    ATTACH 'new.duckdb' AS compact;
    COPY FROM DATABASE boxandone TO compact;

Verified: a 25 MB file holding 50k surviving rows out of 3M compacts to 1 MB
with every row preserved.

This script does that safely — writes to a new file, verifies row counts table
by table, and only then swaps. The original is kept as .prepack until you
delete it yourself.

    python3 compact_db.py
    python3 compact_db.py --keep-original      # do not offer to remove it
"""

import argparse
import os
import sys
import shutil
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
NEW_PATH = DB_PATH + ".compacting"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-original", action="store_true")
    a = ap.parse_args()

    import duckdb

    if not os.path.exists(DB_PATH):
        sys.exit("  %s not found" % DB_PATH)
    if os.path.exists(NEW_PATH):
        os.remove(NEW_PATH)

    before = os.path.getsize(DB_PATH)
    print("  current: %.2f GB" % (before / 1073741824.0))

    # inventory first, so the swap can be verified rather than trusted
    con = duckdb.connect(DB_PATH, read_only=True)
    dbname = con.execute("SELECT current_database()").fetchone()[0]
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]
    counts = {}
    for t in tables:
        counts[t] = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    views = [r[0] for r in con.execute(
        "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
    con.close()

    print("  %d tables, %d views" % (len(tables), len(views)))
    for t in sorted(counts, key=lambda x: -counts[x])[:8]:
        print("    %-22s %12d rows" % (t, counts[t]))

    print("\n  rewriting...")
    con = duckdb.connect(DB_PATH)
    con.execute("ATTACH '%s' AS compact" % NEW_PATH)
    try:
        con.execute("COPY FROM DATABASE %s TO compact" % dbname)
    except Exception as e:
        con.close()
        if os.path.exists(NEW_PATH):
            os.remove(NEW_PATH)
        sys.exit("  FAILED: %s\n  original untouched" % str(e)[:200])
    con.execute("DETACH compact")
    con.close()

    # verify before swapping
    chk = duckdb.connect(NEW_PATH, read_only=True)
    new_tables = [r[0] for r in chk.execute(
        "SELECT table_name FROM duckdb_tables()").fetchall()]
    new_views = [r[0] for r in chk.execute(
        "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
    bad = []
    for t in tables:
        if t not in new_tables:
            bad.append("%s MISSING" % t)
            continue
        n = chk.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        if n != counts[t]:
            bad.append("%s %d -> %d" % (t, counts[t], n))
    chk.close()

    missing_views = [v for v in views if v not in new_views]

    if bad:
        print("\n  VERIFICATION FAILED — not swapping:")
        for b in bad[:10]:
            print("    %s" % b)
        print("  new file left at %s" % NEW_PATH)
        return

    after = os.path.getsize(NEW_PATH)
    print("  verified: %d tables, all row counts match" % len(tables))
    if missing_views:
        print("  note: %d view(s) not carried over: %s"
              % (len(missing_views), ", ".join(missing_views)))
        print("  (re-run the schema files to recreate them)")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old = "%s.prepack_%s" % (DB_PATH, stamp)
    shutil.move(DB_PATH, old)
    shutil.move(NEW_PATH, DB_PATH)

    print("\n  %.2f GB -> %.2f GB  (freed %.2f GB)"
          % (before / 1073741824.0, after / 1073741824.0,
             (before - after) / 1073741824.0))
    print("  original kept: %s" % os.path.basename(old))
    if not a.keep_original:
        print("\n  delete it once you are satisfied:")
        print("    rm %s" % old)


if __name__ == "__main__":
    main()
