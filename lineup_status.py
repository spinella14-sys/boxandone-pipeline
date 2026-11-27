#!/usr/bin/env python3
"""
lineup_status.py — how fresh the lineup data is, and what the last nightly
runs said about it. Reads only; writes nothing.

  1. when each lineup file on R2 was last written: the season's stints and
     player measures, RAPM, a few teams' lineup files, and the newest
     per-game lineup file — against the last game we have a box score for
  2. every lineup-related line from the last few nightly logs

    python3 lineup_status.py          # the last 4 nights
    python3 lineup_status.py 8
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

nights = int(sys.argv[1]) if len(sys.argv) > 1 else 4
env = N.load_env()
s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
season = N.current_season()


def listing(prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += page.get("Contents", [])
    return out


def when(key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)["LastModified"].strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "not on R2"


print("\n  1. lineup files on R2 (season %s)\n" % season)
for o in sorted(listing("v2/lineups/"), key=lambda o: o["Key"]):
    if season in o["Key"] or "/" not in o["Key"][len("v2/lineups/"):]:
        print("    %-52s %s" % (o["Key"], o["LastModified"].strftime("%Y-%m-%d %H:%M UTC")))
print("    %-52s %s" % ("v2/models/rapm.parquet", when("v2/models/rapm.parquet")))
print("    %-52s %s" % ("v2/models/rapm/%s.parquet" % season, when("v2/models/rapm/%s.parquet" % season)))
for t in ("BOS", "DEN", "IND", "LAL", "OKC"):
    k = "v2/teams/%s/lineups/%s.parquet" % (t, season)
    print("    %-52s %s" % (k, when(k)))

games = [o for o in listing("v2/games/") if o["Key"].endswith("/lineups.parquet")]
if games:
    newest = max(games, key=lambda o: o["LastModified"])
    print("    newest per-game lineup file: %s  %s  (%d game files in all)" % (
        newest["Key"], newest["LastModified"].strftime("%Y-%m-%d %H:%M UTC"), len(games)))

try:
    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    last = con.execute("SELECT max(game_date), count(*) FROM games WHERE season = ?", [season]).fetchone()
    print("    last game with a box score in this machine's database: %s (%d games this season)" % last)
    con.close()
except Exception as e:
    print("    (local database not readable: %s)" % str(e)[:80])

print("\n  2. what the last %d nightly runs said about lineups\n" % nights)
since = (datetime.now(timezone.utc) - timedelta(days=nights)).strftime("%Y%m%d")
keys = sorted(o["Key"] for o in listing("v2/logs/nightly/") if os.path.basename(o["Key"]) >= since)
PAT = re.compile(r"build_lineups|export_game_lineups|export_team_lineups|compute_rapm|stint|lineup|"
                 r"games on file|no games on file|reliable|new games?|no new game|rc=|exit|"
                 r"Traceback|Error|FAILED|failed|timed out", re.I)
for k in keys:
    text = s3.get_object(Bucket=bucket, Key=k)["Body"].read().decode(errors="replace")
    print("    --- %s" % os.path.basename(k))
    hits = [l.rstrip() for l in text.splitlines() if PAT.search(l)]
    for l in hits[:60]:
        print("      " + l[:170])
    if not hits:
        print("      (no lineup lines — the lineup steps did not run)")
    print("      last line: " + (text.strip().splitlines() or [""])[-1][:170])
