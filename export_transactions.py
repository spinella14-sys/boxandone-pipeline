#!/usr/bin/env python3
"""
export_transactions.py — transactions to R2, one file per season.

The app reads Parquet from R2 rather than the database, so the transaction log
has to go the same way. One file per season keeps each one small enough to
fetch on demand: a season is roughly a thousand events and three thousand items.

Items are folded into the transaction row as a compact string rather than kept
as a second file, because the page always shows them together and a join across
two fetches would be slower than carrying the text.

    python3 export_transactions.py check
    python3 export_transactions.py build
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")

SQL = """
WITH moves AS (
  SELECT i.transaction_id,
         string_agg(
           CASE WHEN i.item_type = 'player'
                  THEN COALESCE(i.raw_name, '?') || '|' ||
                       COALESCE(i.player_id, '') || '|' ||
                       COALESCE(i.from_team, '') || '|' || COALESCE(i.to_team, '')
                WHEN i.item_type = 'pick'
                  THEN COALESCE(CAST(i.pick_year AS VARCHAR), '?') || ' R' ||
                       COALESCE(CAST(i.pick_round AS VARCHAR), '?') || ' pick||' ||
                       COALESCE(i.from_team, '') || '|' || COALESCE(i.to_team, '')
                WHEN i.item_type = 'pick_became'
                  THEN 'became ' || COALESCE(i.raw_name, '?') || '|' ||
                       COALESCE(i.player_id, '') || '||'
                ELSE i.item_type || '|||' END,
           ';;' ORDER BY i.item_id) AS items
  FROM transaction_items i GROUP BY 1
),
tm AS (
  SELECT transaction_id, string_agg(DISTINCT team_abbr, ',') AS teams
  FROM transaction_teams GROUP BY 1
)
SELECT t.transaction_id, t.txn_date, t.season, t.txn_type, t.raw_text,
       COALESCE(tm.teams, '') AS teams, COALESCE(m.items, '') AS items
FROM transactions t
LEFT JOIN moves m ON m.transaction_id = t.transaction_id
LEFT JOIN tm ON tm.transaction_id = t.transaction_id
WHERE t.season = '%s'
ORDER BY t.txn_date, t.transaction_id
"""


def load_env():
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def seasons(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM transactions ORDER BY season DESC").fetchall()]


def main():
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season", help="export just this season")
    a = ap.parse_args()
    con = duckdb.connect(DB_PATH, read_only=True)
    ss = seasons(con)
    if a.season:
        ss = [s for s in ss if s == a.season]
    if a.cmd == "check":
        print("  %d seasons" % len(ss))
        for s in ss[:3]:
            rows = con.execute(SQL % s).fetchall()
            print("  %-9s %5d transactions" % (s, len(rows)))
            for r in rows[:2]:
                print("     %s %-9s %s" % (r[1], r[3], r[4][:88]))
        con.close()
        return

    env = load_env()
    s3 = s3c(env)
    with tempfile.TemporaryDirectory() as tmp:
        for s in ss:
            p = os.path.join(tmp, "t.parquet")
            if os.path.exists(p):
                os.remove(p)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                        % (SQL % s, p))
            n = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE season=?", [s]).fetchone()[0]
            s3.upload_file(p, env["R2_BUCKET_NAME"],
                           "v2/transactions/%s.parquet" % s)
            print("  %-9s %5d  %5.0f KB" % (s, n, os.path.getsize(p) / 1024))
    con.close()


if __name__ == "__main__":
    main()
