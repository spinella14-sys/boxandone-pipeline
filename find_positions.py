#!/usr/bin/env python3
"""
find_positions.py — where do positions already live? Reads only.

Lists every Supabase table or view with a column whose name contains
"position", with how many rows have it filled, plus the same for the local
DuckDB database.

    python3 find_positions.py
"""
import json
import os
import sys
import urllib.request

HOME = os.path.expanduser("~/boxandone")
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402
import sync_players_supabase as SP  # noqa: E402

env = N.load_env()
base = env["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
req = urllib.request.Request(base, headers=dict(SP.headers(env, None),
                                                Accept="application/openapi+json"))
spec = json.load(urllib.request.urlopen(req, timeout=60))
defs = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
print("Supabase (%d tables and views)" % len(defs))
for table, d in sorted(defs.items()):
    cols = [c for c in (d.get("properties") or {}) if "position" in c.lower()]
    for c in cols:
        code, rows = SP.call(env, None, "%s?select=%s&%s=not.is.null&limit=1" % (table, c, c),
                             prefer="count=exact")
        code2, total = SP.call(env, None, "%s?select=%s&limit=1" % (table, c), prefer="count=exact")
        # PostgREST puts the count in a header SP.call does not return, so count by paging
        n, off = 0, 0
        while code == 200:
            c3, page = SP.call(env, None, "%s?select=%s&%s=not.is.null&limit=1000&offset=%d"
                               % (table, c, c, off))
            if c3 != 200 or not page:
                break
            n += len(page)
            if len(page) < 1000:
                break
            off += 1000
        sample = rows[0] if code == 200 and rows else rows
        print("  %-28s %-22s filled rows: %-6s e.g. %s" % (table, c, n if code == 200 else "HTTP %s" % code,
                                                           str(sample)[:60]))

import duckdb  # noqa: E402
con = duckdb.connect(N.DB_PATH, read_only=True)
print("\nLocal DuckDB")
for t, c in con.execute("""SELECT table_name, column_name FROM duckdb_columns()
                           WHERE lower(column_name) LIKE '%position%' AND database_name = current_database()
                           ORDER BY 1, 2""").fetchall():
    n = con.execute('SELECT COUNT(*) FROM "%s" WHERE "%s" IS NOT NULL' % (t, c)).fetchone()[0]
    print("  %-28s %-22s filled rows: %d" % (t, c, n))
con.close()
