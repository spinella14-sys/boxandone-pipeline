#!/usr/bin/env python3
"""
nightly.py — the whole in-season pipeline, one run.

Designed to run on a cloud runner with no persistent disk. The working database
lives in R2 (70 MB after archiving), gets pulled at the start and pushed at the
end. Everything between is your existing scripts, called as subprocesses, so
the validated code stays the code that runs.

    pull  v2/db/boxandone.duckdb from R2
    ingest_games.py schedule   (current season, cache cleared so new games appear)
    ingest_games.py fetch/parse
    bridge_games.py build      (map the night's games to NBA ids)
    ingest_pbp.py fetch/parse
    bridge_nba_ids.py build    (any players who debuted)
    export: season file, changed player files, registry, manifest
    push  database back to R2
    touch Supabase             (also resets its 7-day inactivity timer)

    python3 nightly.py                 # full run
    python3 nightly.py --dry-run       # report what it would do
    python3 nightly.py --no-pbp        # box scores only
    python3 nightly.py --season 2026-27
    python3 nightly.py --push-db       # upload the local db to R2 (first run)

Reads ~/boxandone/.env, or plain environment variables when running in CI.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DATA = os.path.join(HOME, "data")
DB_PATH = os.path.join(DATA, "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")

DB_KEY = "v2/db/boxandone.duckdb"
LOCK_KEY = "v2/db/.nightly.lock"
LOG_PREFIX = "v2/logs/nightly"
LOCK_STALE_MIN = 90


# ---------------------------------------------------------------------------

def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL",
              "R2_BUCKET_NAME", "SUPABASE_URL", "SUPABASE_ANON_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                           "R2_ENDPOINT_URL", "R2_BUCKET_NAME") if not env.get(k)]
    if missing:
        sys.exit("  missing credentials: %s" % ", ".join(missing))
    return env


def s3c(env):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto",
                        config=Config(retries={"max_attempts": 3}))


def season_end_year(season):
    """'2025-26' -> 2026. ingest_games.py keys on BBRef's end-year URLs."""
    return int(season.split("-")[0]) + 1


def current_season(today=None):
    """NBA seasons span Oct-June. October onward belongs to the new season."""
    d = today or datetime.now()
    y = d.year if d.month >= 10 else d.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


class Log:
    def __init__(self):
        self.lines = []
        self.t0 = time.time()

    def __call__(self, msg):
        stamp = "%6.1fs" % (time.time() - self.t0)
        line = "%s  %s" % (stamp, msg)
        print(line, flush=True)
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


def run(log, argv, cwd=HOME, timeout=3600):
    log("$ %s" % " ".join(argv))
    try:
        p = subprocess.run([sys.executable] + argv, cwd=cwd, timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        log("   TIMEOUT after %ds" % timeout)
        return False, ""
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.strip().splitlines():
        if line.strip() and "PythonDeprecationWarning" not in line \
                and "warnings.warn" not in line:
            log("   %s" % line.rstrip())
    if p.returncode != 0:
        log("   exit %d" % p.returncode)
    return p.returncode == 0, out


# ---------------------------------------------------------------------------

def acquire_lock(s3, bucket, log):
    try:
        obj = s3.get_object(Bucket=bucket, Key=LOCK_KEY)
        held = json.loads(obj["Body"].read())
        started = datetime.fromisoformat(held["started"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        if age < LOCK_STALE_MIN:
            log("another run started %.0f min ago (%s) — exiting"
                % (age, held.get("host", "?")))
            return False
        log("stale lock (%.0f min) — taking it" % age)
    except Exception:
        pass
    s3.put_object(Bucket=bucket, Key=LOCK_KEY, Body=json.dumps({
        "started": datetime.now(timezone.utc).isoformat(),
        "host": os.environ.get("GITHUB_RUN_ID") or os.uname().nodename,
    }).encode())
    return True


def release_lock(s3, bucket):
    try:
        s3.delete_object(Bucket=bucket, Key=LOCK_KEY)
    except Exception:
        pass


def pull_db(s3, bucket, log):
    os.makedirs(DATA, exist_ok=True)
    try:
        head = s3.head_object(Bucket=bucket, Key=DB_KEY)
    except Exception:
        log("no database in R2 at %s — run with --push-db first" % DB_KEY)
        return False
    mb = head["ContentLength"] / 1048576.0
    log("pulling database (%.1f MB)" % mb)
    s3.download_file(bucket, DB_KEY, DB_PATH)
    return True


def push_db(s3, bucket, log):
    mb = os.path.getsize(DB_PATH) / 1048576.0
    log("pushing database (%.1f MB)" % mb)
    s3.upload_file(DB_PATH, bucket, DB_KEY)


def clear_schedule_cache(season, log):
    """BBRef adds box-score links as games finish; cached month pages hide them."""
    end_year = int(season.split("-")[0]) + 1
    d = os.path.join(HOME, "raw", "schedules")
    if not os.path.isdir(d):
        return
    n = 0
    for f in os.listdir(d):
        if f.startswith("NBA_%d_games-" % end_year):
            os.remove(os.path.join(d, f))
            n += 1
    if n:
        log("cleared %d cached schedule pages for %s" % (n, season))


# ---------------------------------------------------------------------------

def changed_players(season, since_games):
    """Players appearing in the games added this run."""
    import duckdb
    if not since_games:
        return []
    con = duckdb.connect(DB_PATH, read_only=True)
    q = ("SELECT DISTINCT player_id FROM player_game_box "
         "WHERE game_id IN (%s)" % ",".join("?" * len(since_games)))
    out = [r[0] for r in con.execute(q, since_games).fetchall()]
    con.close()
    return out


def games_before(season):
    import duckdb
    if not os.path.exists(DB_PATH):
        return set()
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        s = {r[0] for r in con.execute(
            "SELECT game_id FROM games WHERE season=?", [season]).fetchall()}
    except Exception:
        s = set()
    con.close()
    return s


def export_targeted(env, log, season, player_ids):
    """Refresh the season aggregate, the manifest, and only the changed players."""
    sys.path.insert(0, HOME)
    import export_r2 as X
    import tempfile

    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = X.db()
    with tempfile.TemporaryDirectory() as tmp:
        X.put_parquet(con, s3, bucket, X.SEASON_SQL % season,
                      "%s/seasons/%s/player_season.parquet" % (X.PREFIX, season), tmp)
        X.put_parquet(con, s3, bucket, X.TEAM_SEASON_SQL % season,
                      "%s/seasons/%s/team_season.parquet" % (X.PREFIX, season), tmp)
        log("exported season aggregates for %s" % season)

        for i, pid in enumerate(player_ids, 1):
            X.put_parquet(con, s3, bucket, X.GAMELOG_SQL % pid,
                          "%s/players/%s/gamelog.parquet" % (X.PREFIX, pid), tmp)
            X.put_parquet(con, s3, bucket, X.SHOTS_SQL % pid,
                          "%s/players/%s/shots.parquet" % (X.PREFIX, pid), tmp)
        log("exported %d changed player files" % len(player_ids))

        X.put_parquet(con, s3, bucket, X.REGISTRY_SQL,
                      "%s/registry/players.parquet" % X.PREFIX, tmp)
    con.close()
    X.cmd_manifest(env)


def touch_supabase(env, log):
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_ANON_KEY")
    if not url or not key:
        log("supabase not configured — skipping keepalive")
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            "%s/rest/v1/help_levels?select=code&limit=1" % url.rstrip("/"),
            headers={"apikey": key, "Authorization": "Bearer %s" % key})
        with urllib.request.urlopen(req, timeout=30) as r:
            log("supabase touched (HTTP %d)" % r.status)
    except Exception as e:
        log("supabase touch failed: %s" % str(e)[:80])


def write_log(s3, bucket, log, ok):
    key = "%s/%s_%s.txt" % (LOG_PREFIX,
                            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
                            "ok" if ok else "FAIL")
    try:
        s3.put_object(Bucket=bucket, Key=key,
                      Body=log.text().encode(), ContentType="text/plain")
    except Exception:
        pass


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pbp", action="store_true")
    ap.add_argument("--push-db", action="store_true",
                    help="upload the local database to R2 and exit")
    a = ap.parse_args()

    env = load_env()
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    log = Log()
    season = a.season or current_season()

    if a.push_db:
        if not os.path.exists(DB_PATH):
            sys.exit("  %s not found" % DB_PATH)
        push_db(s3, bucket, log)
        log("done")
        return

    log("season %s" % season)

    if a.dry_run:
        before = games_before(season)
        log("database: %s" % ("present" if os.path.exists(DB_PATH) else "absent"))
        log("games already held for %s: %d" % (season, len(before)))
        log("would: schedule, fetch, parse, bridge, pbp, export, push")
        return

    if not acquire_lock(s3, bucket, log):
        return

    ok = True
    try:
        if not pull_db(s3, bucket, log):
            release_lock(s3, bucket)
            return

        before = games_before(season)
        log("holding %d games for %s" % (len(before), season))

        clear_schedule_cache(season, log)
        ey = season_end_year(season)
        ok &= run(log, ["ingest_games.py", "schedule",
                        "--seasons", "%d:%d" % (ey, ey)])[0]
        ok &= run(log, ["ingest_games.py", "fetch"])[0]
        ok &= run(log, ["ingest_games.py", "parse"])[0]

        after = games_before(season)
        new_games = sorted(after - before)
        log("%d new games" % len(new_games))

        if new_games:
            ok &= run(log, ["bridge_games.py", "build",
                            "--seasons", "%s:%s" % (season, season)])[0]
            if not a.no_pbp:
                ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season])[0]
                ok &= run(log, ["ingest_pbp.py", "parse", "--season", season])[0]
            ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])[0]

            pids = changed_players(season, new_games)
            log("%d players to re-export" % len(pids))
            export_targeted(env, log, season, pids)
        else:
            log("nothing new — skipping export")

        push_db(s3, bucket, log)
        touch_supabase(env, log)
        log("done (%s)" % ("ok" if ok else "with errors"))
    except Exception as e:
        ok = False
        log("EXCEPTION: %s" % str(e)[:300])
    finally:
        write_log(s3, bucket, log, ok)
        release_lock(s3, bucket)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
