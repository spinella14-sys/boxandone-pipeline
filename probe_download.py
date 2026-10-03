#!/usr/bin/env python3
"""
probe_download.py — pull a slice of the R2 export to local disk so the frontend
probe can read it without R2 being public yet.

Public bucket access and CORS are a separate configuration problem. This lets
the data contract be tested today and repointed at R2 later by changing one
line in probe.html.

    python3 probe_download.py                    # top 40 players by minutes
    python3 probe_download.py --season 2025-26 --players 80

Writes to ~/boxandone/probe/data/, mirroring the R2 layout exactly, so the
same fetch paths work against either source.
"""

import argparse
import os
import sys

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
OUT = os.path.join(HOME, "probe", "data")
PREFIX = "v2"


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025-26")
    ap.add_argument("--players", type=int, default=40)
    a = ap.parse_args()

    import boto3, duckdb
    env = load_env()
    bucket = env["R2_BUCKET_NAME"]
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")

    def grab(key):
        local = os.path.join(OUT, key[len(PREFIX) + 1:])
        os.makedirs(os.path.dirname(local), exist_ok=True)
        try:
            s3.download_file(bucket, key, local)
            return os.path.getsize(local)
        except Exception as e:
            print("    missing: %s" % key)
            return 0

    total = 0
    total += grab("%s/manifest.json" % PREFIX)
    total += grab("%s/registry/players.parquet" % PREFIX)
    print("  manifest + registry")

    for f in ("player_season", "team_season"):
        n = grab("%s/seasons/%s/%s.parquet" % (PREFIX, a.season, f))
        total += n
        print("  %s %s  %.1f KB" % (a.season, f, n / 1024.0))

    con = duckdb.connect(os.path.join(HOME, "data", "boxandone.duckdb"),
                         read_only=True)
    try:
        ids = [r[0] for r in con.execute("""
            SELECT b.player_id FROM player_game_box b
            JOIN games g ON g.game_id=b.game_id
            WHERE g.season=? AND b.played
            GROUP BY 1 ORDER BY SUM(b.seconds_played) DESC LIMIT ?
        """, [a.season, a.players]).fetchall()]
    except Exception:
        ids = []
    con.close()

    if not ids:
        print("  no players found for %s in the local database" % a.season)
        return

    for pid in ids:
        total += grab("%s/players/%s/gamelog.parquet" % (PREFIX, pid))
        total += grab("%s/players/%s/shots.parquet" % (PREFIX, pid))
    print("  %d players (gamelog + shots)" % len(ids))

    print("\n  %.1f MB -> %s" % (total / 1048576.0, OUT))
    print("\n  serve it:")
    print("    cd ~/boxandone/probe && python3 -m http.server 8080")
    print("  then open http://localhost:8080/probe.html")


if __name__ == "__main__":
    main()
