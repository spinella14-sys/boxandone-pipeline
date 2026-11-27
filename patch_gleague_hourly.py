#!/usr/bin/env python3
"""
patch_gleague_hourly.py — run once.

G League box scores no longer wait for the 5am nightly. The hourly job (at
:37 past every hour, on GitHub) now checks the G League schedule each run and,
when a game has gone final since the last check, fetches and parses its box
score and publishes it: the season's game files and the game logs of the
players in that game. A game that ends at 10:30pm Pacific is on the site by
about 2:40am Eastern, whatever happens to the nightly.

  ingest_gleague.py  `run --only-new`: publish only when new games came in,
                     and only those games' players (the nightly's plain `run`
                     still republishes the whole season once a day)
  hourly.py          runs it; a new G League game counts as a change, so the
                     database is pushed back
  transactions.yml   gives the hourly job the relay's address and token, which
                     it needs to reach NBA.com from GitHub
  last_log.py        new: prints the latest nightly or hourly log, or one
                     step of it, without opening R2

    python3 patch_gleague_hourly.py
    python3 last_log.py nightly "G League"      # what last night's G League step said
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected (%r) — nothing written" % (name, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


patch("ingest_gleague.py", [
    ('''def run_season(season, limit=None, relay=False, export=True):
    con = db()
    try:
        final = cmd_schedule(con, season, relay)
        con.commit()
        if final:
            cmd_fetch(con, season, limit, relay)
            cmd_parse(con, season)
        if export:
            s3, bucket = s3_client()
            cmd_export(con, season, s3, bucket)
    finally:
        con.close()''',
     '''def run_season(season, limit=None, relay=False, export=True, only_new=False):
    """The schedule, then any newly final games. only_new (the hourly job):
    publish only when a game came in, and only that game's players."""
    con = db()
    try:
        held = lambda: {r[0] for r in con.execute(
            "SELECT game_id FROM gl_games WHERE season = ? AND parsed", [season]).fetchall()}
        before = held()
        final = cmd_schedule(con, season, relay)
        con.commit()
        if final:
            cmd_fetch(con, season, limit, relay)
            cmd_parse(con, season)
        new = sorted(held() - before)
        if only_new:
            print("  %s: %d new G League game%s" % (season, len(new), "" if len(new) == 1 else "s"))
        if export and (new or not only_new):
            players = None
            if only_new:
                players = [r[0] for r in con.execute(
                    "SELECT DISTINCT player_id FROM gl_player_game WHERE player_id IS NOT NULL "
                    "AND game_id IN (SELECT unnest(?))", [new]).fetchall()]
            s3, bucket = s3_client()
            cmd_export(con, season, s3, bucket, players)
    finally:
        con.close()'''),
    ('''        run_season(a.season, a.limit, a.relay, not a.no_export)''',
     '''        run_season(a.season, a.limit, a.relay, not a.no_export, a.only_new)'''),
    ('''    ap.add_argument("--no-export", action="store_true")''',
     '''    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--only-new", action="store_true",
                    help="run: publish only when new games came in (the hourly job)")'''),
], "only_new")

patch("hourly.py", [
    ('''        """SELECT player_id, position FROM player_positions
           WHERE source = 'admin' AND ? IS NOT NULL ORDER BY 1""",
    ):''',
     '''        """SELECT player_id, position FROM player_positions
           WHERE source = 'admin' AND ? IS NOT NULL ORDER BY 1""",
        """SELECT game_id FROM gl_games WHERE season = ? AND parsed ORDER BY 1""",
    ):'''),
    ('''        # positions ruled in the site's Positions tool
        ok &= N.run(log, ["positions_supabase.py", "pull"])''',
     '''        # positions ruled in the site's Positions tool
        ok &= N.run(log, ["positions_supabase.py", "pull"])
        # G League finals: box scores up within the hour, not at 5am. Through
        # the relay on GitHub. Its own failure doesn't fail the hour.
        if os.environ.get("NBA_RELAY_URL") or not os.environ.get("GITHUB_RUN_ID"):
            if not N.run(log, ["ingest_gleague.py", "run", "--season", season, "--only-new"]):
                log("G League step failed — the rest of the hour carries on")'''),
], "--only-new")

patch(".github/workflows/transactions.yml", [
    ('''          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
        run: |
          umask 077
          for k in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT_URL \\
                   R2_BUCKET_NAME SUPABASE_URL SUPABASE_ANON_KEY \\
                   SUPABASE_SERVICE_ROLE_KEY; do''',
     '''          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          NBA_RELAY_URL: ${{ secrets.NBA_RELAY_URL }}
          NBA_RELAY_TOKEN: ${{ secrets.NBA_RELAY_TOKEN }}
        run: |
          umask 077
          for k in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT_URL \\
                   R2_BUCKET_NAME SUPABASE_URL SUPABASE_ANON_KEY \\
                   SUPABASE_SERVICE_ROLE_KEY NBA_RELAY_URL NBA_RELAY_TOKEN; do'''),
    ('''      - name: Run
        env:
          BOXANDONE_HOME: /home/runner/boxandone
        run: python ~/boxandone/hourly.py''',
     '''      - name: Run
        env:
          BOXANDONE_HOME: /home/runner/boxandone
          NBA_RELAY_URL: ${{ secrets.NBA_RELAY_URL }}
          NBA_RELAY_TOKEN: ${{ secrets.NBA_RELAY_TOKEN }}
        run: python ~/boxandone/hourly.py'''),
], "NBA_RELAY_URL")

LAST_LOG = r'''#!/usr/bin/env python3
"""
last_log.py — print the latest nightly (or hourly) log from R2.

    python3 last_log.py                       # last night's nightly, whole
    python3 last_log.py nightly "G League"    # only the G League step
    python3 last_log.py hourly                # the latest hourly run
    python3 last_log.py nightly "" 3          # the third most recent nightly
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

kind = sys.argv[1] if len(sys.argv) > 1 else "nightly"
step = sys.argv[2] if len(sys.argv) > 2 else ""
nth = int(sys.argv[3]) if len(sys.argv) > 3 else 1
env = N.load_env()
s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
keys = []
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/logs/%s/" % kind):
    keys += [o["Key"] for o in page.get("Contents", [])]
keys.sort()
if len(keys) < nth:
    sys.exit("  no %s logs" % kind)
print("  recent: " + ", ".join(os.path.basename(k) for k in keys[-5:]))
key = keys[-nth]
text = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
print("  ---- %s\n" % key)
lines = text.splitlines()
if step:
    # from the line that names the step to the next top-level step log line
    start = next((i for i, l in enumerate(lines) if step.lower() in l.lower()), None)
    if start is None:
        print("  no line mentions %r" % step)
        lines = lines[-40:]
    else:
        lines = lines[start:start + 60]
print("\n".join(lines))
'''
p = os.path.join(HOME, "last_log.py")
if not os.path.exists(p):
    open(p, "w").write(LAST_LOG)
    print("  last_log.py written")
