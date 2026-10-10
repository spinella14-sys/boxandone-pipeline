#!/usr/bin/env python3
"""
nightly2.py — the whole in-season pipeline, one unattended run.

Designed for a cloud runner with no persistent disk and no copy of the archive.
The working database lives in R2 and is pulled at the start and pushed at the
end; everything in between is the scripts that have already been validated,
called as subprocesses.

The archive is deliberately avoided rather than downloaded:

    the season aggregate      only needs the current season, which is the whole
                              of the hot database
    a player's career file    is fetched from R2, appended to, and written back
                              — about 20 KB a player rather than 355 MB of
                              archive to recompute from
    registry and manifest     are rebuilt from the thirty season files, which
                              total under 2 MB

So a nightly run moves roughly 100 MB, almost all of it the database itself,
and finishes in a few minutes.

    python3 nightly2.py --dry-run     # report what it would do
    python3 nightly2.py               # the real thing
    python3 nightly2.py --no-pbp      # box scores only, if the NBA feed is down
    python3 nightly2.py --push-db     # seed R2 with the local database, once

A lock in R2 stops two runs colliding, and every run writes a log to
v2/logs/nightly/ marked ok or FAIL so a silent failure at 4am is still visible
in the morning.
"""

import argparse
import json
import os
import subprocess  # patched by patch_nightly
import sys
import tempfile
import time
from datetime import datetime, timezone

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DATA = os.path.join(HOME, "data")
DB_PATH = os.path.join(DATA, "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")

DB_KEY = "v2/db/boxandone.duckdb"
LOCK_KEY = "v2/db/.nightly.lock"
LOG_PREFIX = "v2/logs/nightly"
PREFIX = "v2"
LOCK_STALE_MIN = 120


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
                        config=Config(max_pool_connections=24,
                                      retries={"max_attempts": 3}))


def current_season(today=None):
    """NBA seasons span October to June; October begins the new one."""
    d = today or datetime.now()
    y = d.year if d.month >= 10 else d.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


def season_end_year(season):
    return int(season.split("-")[0]) + 1


class Log:
    def __init__(self):
        self.lines = []
        self.t0 = time.time()

    def __call__(self, msg):
        line = "%6.1fs  %s" % (time.time() - self.t0, msg)
        print(line, flush=True)
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


def run(log, argv, timeout=5400):
    log("$ %s" % " ".join(argv))
    try:
        p = subprocess.run([sys.executable] + argv, cwd=HOME, timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        log("   TIMEOUT after %ds" % timeout)
        return False
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.strip().splitlines():
        if line.strip() and "PythonDeprecationWarning" not in line \
                and "warnings.warn" not in line:
            log("   %s" % line.rstrip())
    if p.returncode != 0:
        log("   exit %d" % p.returncode)
    return p.returncode == 0


# ---------------------------------------------------------------------------

def acquire_lock(s3, bucket, log):
    try:
        held = json.loads(s3.get_object(Bucket=bucket, Key=LOCK_KEY)["Body"].read())
        started = datetime.fromisoformat(held["started"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        if age < LOCK_STALE_MIN:
            log("another run started %.0f min ago (%s) — exiting"
                % (age, held.get("host", "?")))
            return False
        log("stale lock (%.0f min old) — taking it" % age)
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
        log("no database at %s — seed it first with --push-db" % DB_KEY)
        return False
    log("pulling database (%.0f MB)" % (head["ContentLength"] / 1048576.0))
    s3.download_file(bucket, DB_KEY, DB_PATH)
    return True


def push_db(s3, bucket, log):
    log("pushing database (%.0f MB)" % (os.path.getsize(DB_PATH) / 1048576.0))
    s3.upload_file(DB_PATH, bucket, DB_KEY)


def clear_schedule_cache(season, log):
    """Basketball Reference adds box-score links as games finish, so a cached
    month page hides last night's results. Without this the job would run
    cleanly every night and never find a new game."""
    end = season_end_year(season)
    d = os.path.join(HOME, "raw", "schedules")
    if not os.path.isdir(d):
        return
    n = 0
    for f in os.listdir(d):
        if f.startswith("NBA_%d_games-" % end):
            os.remove(os.path.join(d, f))
            n += 1
    if n:
        log("cleared %d cached schedule pages for %s" % (n, season))


def games_held(season):
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


# ---------------------------------------------------------------------------
# export, without touching the archive
# ---------------------------------------------------------------------------

def export_season(env, log, season):
    """Season aggregates come entirely from the hot database — the archive holds
    no rows for a season still being played."""
    sys.path.insert(0, HOME)
    import duckdb
    import export_full as X

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    for view, tbl in (("all_games", "games"), ("all_box", "player_game_box"),
                      ("all_pbp", "play_by_play")):
        con.execute("CREATE VIEW %s AS SELECT * FROM hot.%s" % (view, tbl))

    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    with tempfile.TemporaryDirectory() as tmp:
        for sql, key, args in (
                (X.SEASON_SQL, "player_season", (season, season, season)),
                (X.TEAM_SQL, "team_season", (season, season, season, season)),
                (X.PLAYER_GAME_SQL, "player_game", (season,))):
            n = X.put(con, s3, bucket, sql % args,
                              "%s/seasons/%s/%s.parquet" % (PREFIX, season, key), tmp)
            log("  %s %.0f KB" % (key, (n or 0) / 1024.0))
    con.close()


def export_changed_players(env, log, season, player_ids):
    """A career file is fetched, appended to and written back, rather than
    rebuilt from thirty seasons of archive. The new rows are the only ones the
    hot database holds, which is exactly what needs adding."""
    if not player_ids:
        log("no players to update")
        return
    sys.path.insert(0, HOME)
    import duckdb
    import export_full as X

    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    for view, tbl in (("all_games", "games"), ("all_box", "player_game_box"),
                      ("all_pbp", "play_by_play")):
        con.execute("CREATE VIEW %s AS SELECT * FROM hot.%s" % (view, tbl))

    done = 0
    with tempfile.TemporaryDirectory() as tmp:
        for kind, sql in (("gamelog", X.GAMELOG_SQL), ("shots", X.SHOTS_SQL)):
            for pid in player_ids:
                key = "%s/players/%s/%s.parquet" % (PREFIX, pid, kind)
                new = os.path.join(tmp, "new.parquet")
                if os.path.exists(new):
                    os.remove(new)
                con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
                            % (sql % pid, new))

                old = os.path.join(tmp, "old.parquet")
                have_old = False
                try:
                    s3.download_file(bucket, key, old)
                    have_old = True
                except Exception:
                    pass

                out = os.path.join(tmp, "out.parquet")
                if os.path.exists(out):
                    os.remove(out)
                if have_old:
                    # the career file keeps every season; the hot database only
                    # has the current one, so rows are merged rather than replaced
                    con.execute("""
                        COPY (SELECT * FROM read_parquet('%s')
                              WHERE season <> '%s'
                              UNION ALL BY NAME
                              SELECT * FROM read_parquet('%s'))
                        TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"""
                        % (old, season, new, out))
                else:
                    out = new
                s3.upload_file(out, bucket, key)
                done += 1
            log("  %s updated for %d players" % (kind, len(player_ids)))
    con.close()


def rebuild_registry_and_manifest(env, log):
    """Career totals and the manifest are summed from the thirty season files,
    which is far cheaper than reading the archive and gives the same answer."""
    sys.path.insert(0, HOME)
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]

    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": "%s/seasons/" % PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])
                 if o["Key"].endswith("player_season.parquet")]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        local = []
        for k in keys:
            p = os.path.join(tmp, k.replace("/", "_"))
            s3.download_file(bucket, k, p)
            local.append(p)
        if not local:
            log("no season files found — registry not rebuilt")
            con.close()
            return
        glob = os.path.join(tmp, "*player_season.parquet")

        reg = os.path.join(tmp, "registry.parquet")
        # What a source claims about a player lives in player_bio, kept apart
        # from the registry so a scrape never overwrites a better-trusted
        # value. The registry wins; the scrape only fills blanks. Same rule
        # as export_full.REGISTRY_SQL.
        has_bio = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() "
            "WHERE database_name='hot' AND table_name='player_bio'").fetchone()[0] > 0
        if has_bio:
            cols = """
                COALESCE(p.birthdate, bio.birthdate) AS birthdate,
                CASE WHEN p.birthdate IS NULL AND bio.birthdate IS NOT NULL
                     THEN 'confirmed' ELSE p.birthdate_status END AS birthdate_status,
                p.position,
                COALESCE(p.height_in, bio.height_in) AS height_in,
                COALESCE(p.weight_lb, bio.weight_lb) AS weight_lb,
                COALESCE(p.college, bio.college) AS college,
                COALESCE(p.draft_year, bio.draft_year) AS draft_year,
                COALESCE(p.draft_round, bio.draft_round) AS draft_round,
                COALESCE(p.draft_pick, bio.draft_pick) AS draft_pick,
                p.status,
                COALESCE(p.nationality, bio.country) AS nationality"""
            join = ("LEFT JOIN hot.player_bio bio "
                    "ON bio.player_id=p.player_id AND bio.source='nba'")
        else:
            log("  player_bio not in this database — registry without enrichment")
            cols = """
                p.birthdate, p.birthdate_status, p.position,
                p.height_in, p.weight_lb, p.college, p.draft_year,
                p.draft_round, p.draft_pick, p.status, p.nationality"""
            join = ""
        sql = ("""
            COPY (
              SELECT p.player_id, p.full_name, p.display_name, p.name_normalized,
                     """ + cols + """,
                     i.source_id AS bbref_id, n.source_id AS nba_id,
                     s.seasons, s.first_season, s.last_season,
                     s.career_gp, s.career_pts
              FROM hot.players p
              LEFT JOIN hot.player_identifiers i
                ON i.player_id=p.player_id AND i.source='bbref'
              LEFT JOIN hot.player_identifiers n
                ON n.player_id=p.player_id AND n.source='nba'
              """ + join + """
              LEFT JOIN (
                SELECT player_id,
                       COUNT(DISTINCT season) AS seasons,
                       MIN(season) AS first_season, MAX(season) AS last_season,
                       SUM(gp) AS career_gp, SUM(pts) AS career_pts
                FROM read_parquet('%s', union_by_name=true)
                WHERE season_type='regular'
                GROUP BY 1
              ) s ON s.player_id = p.player_id
            ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        con.execute(sql % (glob, reg))
        import rosters_export as RX
        reg = RX.add_rosters(con, reg, tmp)
        s3.upload_file(reg, bucket, "%s/registry/players.parquet" % PREFIX)
        log("  registry %.0f KB" % (os.path.getsize(reg) / 1024.0))

        rows = con.execute("""
            SELECT season, SUM(gp) AS gp, SUM(pts) AS pts,
                   COUNT(DISTINCT player_id) AS players
            FROM read_parquet('%s', union_by_name=true)
            GROUP BY 1 ORDER BY 1 DESC""" % glob).fetchall()
        games = {r[0]: int(r[1] or 0) for r in rows}
        box = con.execute("SELECT COUNT(*) FROM hot.player_game_box").fetchone()[0]
        pbp = con.execute("SELECT COUNT(*) FROM hot.play_by_play").fetchone()[0]
        man = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "prefix": PREFIX,
            "seasons": [{"season": r[0], "games": int(r[1] or 0)} for r in rows],
            "players_with_games": con.execute(
                "SELECT COUNT(DISTINCT player_id) FROM read_parquet('%s', union_by_name=true)"
                % glob).fetchone()[0],
            "box_rows": box, "pbp_events": pbp,
            "paths": {
                "registry": "%s/registry/players.parquet" % PREFIX,
                "player_season": "%s/seasons/{season}/player_season.parquet" % PREFIX,
                "team_season": "%s/seasons/{season}/team_season.parquet" % PREFIX,
                "player_game": "%s/seasons/{season}/player_game.parquet" % PREFIX,
                "gamelog": "%s/players/{player_id}/gamelog.parquet" % PREFIX,
                "shots": "%s/players/{player_id}/shots.parquet" % PREFIX,
            },
        }
        s3.put_object(Bucket=bucket, Key="%s/manifest.json" % PREFIX,
                      Body=json.dumps(man, indent=2).encode(),
                      ContentType="application/json")
        log("  manifest: %d seasons" % len(rows))
    con.close()


def box_keys(season):
    """(player, game) pairs held for a season, to see exactly whose lines
    a run added."""
    import duckdb
    if not os.path.exists(DB_PATH):
        return set()
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        out = set(con.execute("""SELECT b.player_id, b.game_id
                                 FROM player_game_box b
                                 JOIN games g ON g.game_id = b.game_id
                                 WHERE g.season = ?""", [season]).fetchall())
    except Exception:
        out = set()
    con.close()
    return out


def export_rosters(env, log):
    sys.path.insert(0, HOME)
    import duckdb
    import rosters_export as RX
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    r = RX.export_current(con, s3c(env), env["R2_BUCKET_NAME"])
    con.close()
    log("  rosters: %s" % ("none on file" if not r else
                           "%d players as of %s" % (r[1], r[0])))


def box_rows(season):
    """Box-score rows held for a season. A preseason game first ingested with
    some players unknown gains rows later, once rosters add them, without
    becoming a new game; this is how that is noticed."""
    import duckdb
    if not os.path.exists(DB_PATH):
        return 0
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        n = con.execute("""SELECT COUNT(*) FROM player_game_box b
                          JOIN games g ON g.game_id = b.game_id
                          WHERE g.season = ?""", [season]).fetchone()[0]
    except Exception:
        n = 0
    con.close()
    return n


def changed_players(season, new_games):
    import duckdb
    if not new_games:
        return []
    con = duckdb.connect(DB_PATH, read_only=True)
    q = ("SELECT DISTINCT player_id FROM player_game_box WHERE game_id IN (%s)"
         % ",".join("?" * len(new_games)))
    out = [r[0] for r in con.execute(q, list(new_games)).fetchall()]
    con.close()
    return out


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
            log("supabase touched (HTTP %d) — also resets the pause timer" % r.status)
    except Exception as e:
        log("supabase touch failed: %s" % str(e)[:80])


def write_log(s3, bucket, log, ok):
    key = "%s/%s_%s.txt" % (LOG_PREFIX,
                            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
                            "ok" if ok else "FAIL")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=log.text().encode(),
                      ContentType="text/plain")
    except Exception:
        pass


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pbp", action="store_true")
    ap.add_argument("--push-db", action="store_true")
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
        log("database present: %s" % os.path.exists(DB_PATH))
        log("games held for %s: %d" % (season, len(games_held(season))))
        log("would: schedule, fetch, parse, bridge, pbp, possessions, export, push")
        return

    if not acquire_lock(s3, bucket, log):
        # say so in R2 rather than exiting without a trace
        try:
            s3.put_object(Bucket=bucket, Key="%s/%s_locked.txt" % (
                LOG_PREFIX, datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")),
                Body=log.text().encode(), ContentType="text/plain")
        except Exception:
            pass
        return

    ok = True
    try:
        if not pull_db(s3, bucket, log):
            release_lock(s3, bucket)
            return

        before = games_held(season)
        log("holding %d games for %s" % (len(before), season))

        ey = season_end_year(season)
        clear_schedule_cache(season, log)
        ok &= run(log, ["ingest_games.py", "schedule", "--seasons", "%d:%d" % (ey, ey)])
        ok &= run(log, ["ingest_games.py", "fetch"])
        ok &= run(log, ["ingest_games.py", "parse"])

        after = games_held(season)
        new_games = sorted(after - before)
        log("%d new games" % len(new_games))

        # NBA.com sources Basketball Reference does not carry. Rosters first,
        # so a rookie exists before his first preseason box score arrives.
        rows_before = box_keys(season)
        ok &= run(log, ["ingest_rosters.py", "build", "--season", season])
        # new players must exist in Supabase before a scout can tag them
        ok &= run(log, ["sync_players_supabase.py"])
        ok &= run(log, ["ingest_schedule.py", "build"])
        if datetime.now().month in (9, 10):
            ok &= run(log, ["ingest_preseason.py", "build", "--season", season])
        pre_games = sorted(games_held(season) - after)
        new_rows = box_keys(season) - rows_before
        grew = bool(new_rows)
        log("%d new preseason games" % len(pre_games))

        ok &= run(log, ["ingest_transactions.py", "build", "--season", season, "--force"])
        ok &= run(log, ["export_transactions.py", "build"])

        if new_games or pre_games or grew:
            if new_games:
                ok &= run(log, ["bridge_games.py", "build",
                                "--seasons", "%s:%s" % (season, season)])
                if not a.no_pbp:
                    ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season])
                    ok &= run(log, ["ingest_pbp.py", "parse", "--season", season])
                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])

            log("exporting season aggregates")
            export_season(env, log, season)
            # export_season rewrites player_season without BPM; put it back
            ok &= run(log, ["compute_bpm.py", "--season", season])
            ok &= run(log, ["export_games_index.py", "build", "--season", season])

            # everyone in a new game, plus anyone whose lines changed in a
            # game we already held (a rookie added after his debut)
            pids = sorted(set(changed_players(season, new_games))
                          | {p for p, _ in new_rows})
            log("exporting %d changed player files" % len(pids))
            export_changed_players(env, log, season, pids)

            log("parsing possessions")
            ok &= run(log, ["parse_possessions_full.py", "build", "--season", season])
        else:
            log("nothing new — skipping export")

        # Signings and waivers happen on days without games, so who is on
        # which team is rebuilt every night. A failure here is logged and the
        # run still pushes the database, so the night's ingest is not lost.
        try:
            log("rebuilding registry and manifest")
            rebuild_registry_and_manifest(env, log)
            export_rosters(env, log)
        except Exception as e:
            ok = False
            log("registry/rosters FAILED: %s" % str(e)[:300])

        push_db(s3, bucket, log)
        touch_supabase(env, log)
        log("done (%s)" % ("ok" if ok else "with errors"))
    except Exception as e:
        ok = False
        import traceback
        log("EXCEPTION: %s" % str(e)[:300])
        for line in traceback.format_exc().splitlines()[-6:]:
            log("   %s" % line)
    finally:
        write_log(s3, bucket, log, ok)
        release_lock(s3, bucket)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
