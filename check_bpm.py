#!/usr/bin/env python3
"""
check_bpm.py — read-only. Does each season's player_season.parquet on R2
still carry BPM, and when was it last written?

    python3 check_bpm.py                   # 2026-27 and 2025-26
    python3 check_bpm.py 2024-25 2023-24

Writes nothing, locally or to R2.
"""
import os
import sys
import tempfile

import boto3
import duckdb

SEASONS = sys.argv[1:] or ["2026-27", "2025-26"]
ENV_PATH = os.path.expanduser("~/boxandone/.env")

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

with tempfile.TemporaryDirectory() as tmp:
    for s in SEASONS:
        key = "v2/seasons/%s/player_season.parquet" % s
        local = os.path.join(tmp, "%s.parquet" % s)
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            s3.download_file(bucket, key, local)
        except Exception as e:
            print("  %s  player_season MISSING (%s)" % (s, type(e).__name__))
            continue
        con = duckdb.connect()
        cols = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet('%s')" % local).fetchall()]
        print("  %s  last written %s" % (s, head["LastModified"]))
        if "bpm" not in cols:
            print("      no bpm column at all")
        else:
            for st, n, have in con.execute("""
                    SELECT season_type, COUNT(*), COUNT(bpm)
                    FROM read_parquet('%s') GROUP BY 1 ORDER BY 1""" % local).fetchall():
                print("      %-11s %4d players, %4d with bpm" % (st, n, have))
        con.close()
