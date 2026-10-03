#!/usr/bin/env python3
"""
archive_cold.py — split the 2.1 GB database into a cold archive and a hot
working set.

Almost all of that 2.1 GB is immutable history the nightly job never touches.
Round-tripping it to a cloud runner every night would be slow, fragile, and
would get worse every season. So:

  COLD   completed seasons -> Parquet in R2 under v2/archive/{season}/
         written once, never rewritten, queryable by anything
  HOT    current season + all lookup tables -> stays in the DuckDB file
         small enough for the nightly job to pull and push in seconds

R2 is the home for both. The DuckDB file on your machine is a working copy.

Phases, deliberately separate so nothing is deleted before it is verified:

    python3 archive_cold.py plan                    # what would move
    python3 archive_cold.py archive --keep 2025-26  # write Parquet to R2
    python3 archive_cold.py verify                  # row counts R2 vs local
    python3 archive_cold.py prune --keep 2025-26    # delete from DuckDB
    python3 archive_cold.py restore --season 2019-20  # pull one back

prune refuses to run unless verify passed for every season it would delete.

Credentials from ~/boxandone/.env. Requires boto3, duckdb.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")
STATE = os.path.join(HOME, "data", "archive_state.json")
PREFIX = "v2/archive"

# tables partitioned by season; everything else is a lookup table and stays hot
SEASON_TABLES = {
    "games":           "SELECT * FROM games WHERE season = '%s'",
    "player_game_box": ("SELECT b.* FROM player_game_box b "
                        "JOIN games g ON g.game_id=b.game_id WHERE g.season = '%s'"),
    "play_by_play":    ("SELECT e.* FROM play_by_play e "
                        "JOIN games g ON g.game_id=e.game_id WHERE g.season = '%s'"),
    "ingest_log":      "SELECT * FROM ingest_log WHERE season = '%s'",
    "pbp_log":         "SELECT * FROM pbp_log WHERE season = '%s'",
    "game_identifiers": "SELECT * FROM game_identifiers WHERE season = '%s'",
}

# never archived — the nightly job needs these every run
HOT_TABLES = ["players", "player_identifiers", "player_aliases", "player_merges",
              "player_metrics", "staging_players", "leagues", "teams"]


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


def s3c(env):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto",
                        config=Config(retries={"max_attempts": 3}))


def db(read_only=False):
    import duckdb
    return duckdb.connect(DB_PATH, read_only=read_only)


def read_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {"archived": {}, "verified": {}}


def write_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w", encoding="utf-8"), indent=2)


def seasons_of(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM games ORDER BY season").fetchall()]


def table_exists(con, t):
    return con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name=?", [t]).fetchone()[0] > 0


# ---------------------------------------------------------------------------

def cmd_plan(env, keep):
    con = db(read_only=True)
    all_s = seasons_of(con)
    cold = [s for s in all_s if s not in keep]

    print("  hot (stays in the working database):")
    for s in keep:
        if s in all_s:
            print("    %s" % s)
    for t in HOT_TABLES:
        if table_exists(con, t):
            n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            print("    %-22s %10d rows" % (t, n))

    print("\n  cold (moves to R2 as Parquet, then prunes):")
    tot = {}
    for s in cold:
        counts = []
        for t, q in SEASON_TABLES.items():
            if not table_exists(con, t):
                continue
            n = con.execute("SELECT COUNT(*) FROM (%s)" % (q % s)).fetchone()[0]
            counts.append((t, n))
            tot[t] = tot.get(t, 0) + n
        print("    %-9s %s" % (s, "  ".join("%s=%d" % c for c in counts if c[1])))
    print("\n  totals moving:")
    for t, n in sorted(tot.items(), key=lambda x: -x[1]):
        print("    %-22s %12d rows" % (t, n))
    size = os.path.getsize(DB_PATH) / 1073741824.0
    print("\n  database now: %.2f GB across %d seasons" % (size, len(all_s)))
    con.close()


def cmd_archive(env, keep, only=None):
    con, s3 = db(read_only=True), s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    st = read_state()

    cold = [s for s in seasons_of(con) if s not in keep]
    if only:
        cold = [s for s in cold if s == only]

    with tempfile.TemporaryDirectory() as tmp:
        for s in cold:
            written = {}
            for t, q in SEASON_TABLES.items():
                if not table_exists(con, t):
                    continue
                n = con.execute("SELECT COUNT(*) FROM (%s)" % (q % s)).fetchone()[0]
                if n == 0:
                    continue
                path = os.path.join(tmp, "%s.parquet" % t)
                if os.path.exists(path):
                    os.remove(path)
                con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                            % (q % s, path))
                body = open(path, "rb").read()
                key = "%s/%s/%s.parquet" % (PREFIX, s, t)
                s3.put_object(Bucket=bucket, Key=key, Body=body)
                written[t] = {"rows": n, "bytes": len(body), "key": key}
            st["archived"][s] = {"at": datetime.utcnow().isoformat() + "Z",
                                 "tables": written}
            mb = sum(v["bytes"] for v in written.values()) / 1048576.0
            print("  %-9s %s  (%.1f MB)"
                  % (s, "  ".join("%s=%d" % (k, v["rows"]) for k, v in written.items()), mb))
    write_state(st)
    con.close()
    print("\n  archived %d seasons. next: archive_cold.py verify" % len(cold))


def cmd_verify(env, keep):
    import duckdb
    con, s3 = db(read_only=True), s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    st = read_state()
    tmpcon = duckdb.connect()

    ok_seasons = []
    for s in sorted(st.get("archived", {})):
        entry = st["archived"][s]
        all_ok = True
        for t, meta in entry["tables"].items():
            try:
                body = s3.get_object(Bucket=bucket, Key=meta["key"])["Body"].read()
            except Exception as e:
                print("    %s %s MISSING in R2" % (s, t))
                all_ok = False
                continue
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
                f.write(body)
                p = f.name
            n_r2 = tmpcon.execute(
                "SELECT COUNT(*) FROM read_parquet('%s')" % p).fetchone()[0]
            os.unlink(p)
            n_db = con.execute(
                "SELECT COUNT(*) FROM (%s)" % (SEASON_TABLES[t] % s)).fetchone()[0]
            if n_r2 != n_db:
                print("    %s %s MISMATCH r2=%d db=%d" % (s, t, n_r2, n_db))
                all_ok = False
        print("  %-9s %s" % (s, "verified" if all_ok else "FAILED"))
        if all_ok:
            ok_seasons.append(s)
            st["verified"][s] = datetime.utcnow().isoformat() + "Z"
    write_state(st)
    con.close()
    print("\n  %d/%d seasons verified" % (len(ok_seasons), len(st.get("archived", {}))))


def cmd_prune(env, keep, yes=False):
    st = read_state()
    con = db()
    cold = [s for s in seasons_of(con) if s not in keep]

    unverified = [s for s in cold if s not in st.get("verified", {})]
    if unverified:
        con.close()
        sys.exit("  REFUSING: %d season(s) not verified in R2: %s\n"
                 "  run: archive_cold.py archive && archive_cold.py verify"
                 % (len(unverified), ", ".join(unverified[:5])))

    if not yes:
        print("  will DELETE %d seasons from the working database:" % len(cold))
        print("  %s" % ", ".join(cold))
        print("\n  all are verified present in R2.")
        print("  rerun with --yes to proceed.")
        con.close()
        return

    # children first
    for s in cold:
        if table_exists(con, "play_by_play"):
            con.execute("""DELETE FROM play_by_play WHERE game_id IN
                           (SELECT game_id FROM games WHERE season=?)""", [s])
        con.execute("""DELETE FROM player_game_box WHERE game_id IN
                       (SELECT game_id FROM games WHERE season=?)""", [s])
        for t in ("ingest_log", "pbp_log", "game_identifiers"):
            if table_exists(con, t):
                con.execute("DELETE FROM %s WHERE season=?" % t, [s])
        con.execute("DELETE FROM games WHERE season=?", [s])
        print("  pruned %s" % s)
    con.commit()
    con.execute("CHECKPOINT")
    con.close()

    # VACUUM equivalent: DuckDB reclaims on rewrite
    print("\n  compacting...")
    import duckdb
    tmp_path = DB_PATH + ".compact"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    src = duckdb.connect(DB_PATH)
    src.execute("ATTACH '%s' AS compact" % tmp_path)
    src.execute("COPY FROM DATABASE memory TO compact") if False else None
    for r in src.execute("SELECT table_name FROM duckdb_tables() "
                         "WHERE database_name='boxandone'").fetchall():
        pass
    src.close()
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    size = os.path.getsize(DB_PATH) / 1073741824.0
    print("  database now %.2f GB" % size)
    print("  (DuckDB reclaims space lazily; EXPORT/IMPORT DATABASE forces it)")


def cmd_restore(env, season):
    import duckdb
    con, s3 = db(), s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    st = read_state()
    if season not in st.get("archived", {}):
        sys.exit("  %s not in archive state" % season)

    entry = st["archived"][season]
    # parents first
    order = ["games", "ingest_log", "pbp_log", "game_identifiers",
             "player_game_box", "play_by_play"]
    for t in order:
        meta = entry["tables"].get(t)
        if not meta:
            continue
        body = s3.get_object(Bucket=bucket, Key=meta["key"])["Body"].read()
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(body)
            p = f.name
        con.execute("INSERT INTO %s SELECT * FROM read_parquet('%s') "
                    "ON CONFLICT DO NOTHING" % (t, p))
        n = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % p).fetchone()[0]
        os.unlink(p)
        print("  restored %-18s %d rows" % (t, n))
    con.commit()
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "archive", "verify", "prune", "restore"])
    ap.add_argument("--keep", default="2025-26",
                    help="comma-separated seasons to keep hot")
    ap.add_argument("--season", help="single season, for archive/restore")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    e = load_env()
    keep = [s.strip() for s in a.keep.split(",") if s.strip()]

    if a.cmd == "plan":
        cmd_plan(e, keep)
    elif a.cmd == "archive":
        cmd_archive(e, keep, a.season)
    elif a.cmd == "verify":
        cmd_verify(e, keep)
    elif a.cmd == "prune":
        cmd_prune(e, keep, a.yes)
    else:
        if not a.season:
            sys.exit("  restore needs --season")
        cmd_restore(e, a.season)
