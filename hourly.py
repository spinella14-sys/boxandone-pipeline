#!/usr/bin/env python3
"""
hourly.py — transactions and rosters, every hour.

The nightly job does everything; this does only what cannot wait for it:

  1. this season's Basketball Reference transactions page (one request)
  2. all thirty NBA.com rosters (thirty requests), so a trade or waiver moves
     the player to his new team or to free agency within the hour
  3. if either changed: the season's transactions file, the registry
     (current team / free agent) and the current-roster file, then the
     database goes back to R2

If nothing changed it pushes nothing and says so. It takes the same lock as
the nightly job, and the two workflows share a GitHub concurrency group, so
they never run on top of each other.

Logs go to v2/logs/hourly/.

    python3 hourly.py
"""
import hashlib
import os
import sys
from datetime import datetime, timezone

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

LOG_PREFIX = "v2/logs/hourly"


def fingerprint(season):
    """A hash of what this job can change, to tell whether anything did."""
    import duckdb
    con = duckdb.connect(N.DB_PATH, read_only=True)
    h = hashlib.sha1()
    for sql in (
        """SELECT transaction_id, raw_text FROM transactions
           WHERE season = ? ORDER BY 1""",
        """SELECT team_abbr, player_id FROM team_rosters
           WHERE as_of = (SELECT MAX(as_of) FROM team_rosters) AND season = ?
           ORDER BY 1, 2""",
    ):
        try:
            for row in con.execute(sql, [season]).fetchall():
                h.update(repr(row).encode())
        except Exception:
            h.update(b"missing")
    con.close()
    return h.hexdigest()


def write_log(s3, bucket, log, status):
    key = "%s/%s_%s.txt" % (LOG_PREFIX,
                            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"), status)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=log.text().encode(),
                      ContentType="text/plain")
    except Exception:
        pass


def main():
    env = N.load_env()
    s3 = N.s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    log = N.Log()
    season = N.current_season()
    log("hourly %s" % season)

    if not N.acquire_lock(s3, bucket, log):
        write_log(s3, bucket, log, "locked")
        return

    status = "ok"
    try:
        if not N.pull_db(s3, bucket, log):
            status = "FAIL"
            return
        before = fingerprint(season)
        ok = True
        ok &= N.run(log, ["ingest_transactions.py", "build", "--season", season, "--force"])
        ok &= N.run(log, ["ingest_rosters.py", "build", "--season", season])
        if fingerprint(season) == before:
            log("no change — nothing pushed")
            status = "ok" if ok else "FAIL"
            return

        log("changed — exporting")
        ok &= N.run(log, ["export_transactions.py", "build", "--season", season])
        try:
            N.rebuild_registry_and_manifest(env, log)
            N.export_rosters(env, log)
        except Exception as e:
            ok = False
            log("registry/rosters FAILED: %s" % str(e)[:300])
        N.push_db(s3, bucket, log)
        log("done (%s)" % ("ok" if ok else "with errors"))
        status = "ok" if ok else "FAIL"
    except Exception as e:
        status = "FAIL"
        log("EXCEPTION: %s" % str(e)[:300])
    finally:
        write_log(s3, bucket, log, status)
        N.release_lock(s3, bucket)
    if status == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
