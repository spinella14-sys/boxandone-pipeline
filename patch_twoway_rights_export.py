#!/usr/bin/env python3
"""
patch_twoway_rights_export.py — run once.

Exports every stored G League move, all seasons, to
v2/gleague/transactions/all.parquet as well as this season's file. A player's
page reads it to show who holds his G League rights and how they have moved;
rights outlast a season, so one season's file isn't enough.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "transactions/all.parquet" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()
OLD = '''                "gleague/transactions/%s.parquet" % season)'''
NEW = '''                "gleague/transactions/%s.parquet" % season)
        put_sql("SELECT * EXCLUDE (seen_at) FROM gl_transactions ORDER BY txn_date DESC, raw_text",
                "gleague/transactions/all.parquet")'''
if src.count(OLD) != 1:
    sys.exit("  ingest_twoway.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  ingest_twoway.py patched: all G League moves exported for player pages")
