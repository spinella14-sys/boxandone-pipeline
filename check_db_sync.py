#!/usr/bin/env python3
"""
check_db_sync.py — read-only. Is the database the nightly job uses (R2,
v2/db/boxandone.duckdb) the same as the one on this Mac?

The nightly job pulls the R2 copy, adds to it and pushes it back. Anything done
only to the local copy (rosters, enrichment, preseason, transactions) is
invisible to it, and whichever side pushes last overwrites the other.

    python3 check_db_sync.py

Downloads the R2 copy to a temp file, compares row counts table by table, and
lists the last few nightly logs. Writes nothing.
"""
import os
import sys
import tempfile

import boto3
import duckdb

HOME = os.path.expanduser("~/boxandone")
LOCAL = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")

env = {}
for line in open(ENV_PATH, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                  aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                  region_name="auto")
bucket = env["R2_BUCKET_NAME"]


def counts(path):
    con = duckdb.connect(path, read_only=True)
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_type='BASE TABLE' ORDER BY 1").fetchall()]
    out = {}
    for t in tables:
        out[t] = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    latest = None
    if "games" in tables:
        latest = con.execute("SELECT MAX(game_date) FROM games").fetchone()[0]
    con.close()
    return out, latest


head = s3.head_object(Bucket=bucket, Key="v2/db/boxandone.duckdb")
print("\n  R2 database    last pushed %s  (%.0f MB)"
      % (head["LastModified"], head["ContentLength"] / 1048576.0))
print("  local database last changed %s  (%.0f MB)"
      % (__import__("datetime").datetime.fromtimestamp(os.path.getmtime(LOCAL)),
         os.path.getsize(LOCAL) / 1048576.0))

with tempfile.TemporaryDirectory() as tmp:
    remote_path = os.path.join(tmp, "r2.duckdb")
    s3.download_file(bucket, "v2/db/boxandone.duckdb", remote_path)
    remote, r_latest = counts(remote_path)
local, l_latest = counts(LOCAL)

print("\n  %-26s %12s %12s" % ("table", "local", "R2"))
same = True
for t in sorted(set(local) | set(remote)):
    l, r = local.get(t), remote.get(t)
    flag = "" if l == r else "   <- local only" if r is None else \
           "   <- R2 only" if l is None else "   <- differs"
    if flag:
        same = False
    print("  %-26s %12s %12s%s" % (t, "—" if l is None else "{:,}".format(l),
                                    "—" if r is None else "{:,}".format(r), flag))
print("\n  latest game date   local %s   R2 %s" % (l_latest, r_latest))

print("\n  last nightly logs:")
page = s3.list_objects_v2(Bucket=bucket, Prefix="v2/logs/nightly/")
for o in sorted(page.get("Contents", []), key=lambda o: o["Key"])[-5:]:
    print("    %s" % o["Key"].split("/")[-1])
print("\n  %s\n" % ("identical row counts" if same else "the two databases differ"))
