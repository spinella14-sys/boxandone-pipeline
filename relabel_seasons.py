#!/usr/bin/env python3
"""
relabel_seasons.py — fix season_type across the archived seasons.

patch_season_type.py and patch_playin_fix.py ran when only 2025-26 was in the
database. The 29-season backfill came afterward and inserted every game with
season_type defaulting to 'regular', so playoff, play-in and Cup games from
1997 through 2024 are all filed as regular season. The archive and the R2
export both captured that.

This works directly on the archived Parquet — no need to restore 2.1 GB back
into DuckDB. Same rules as the original patches:

  playoff     from BBRef's playoff index (authoritative)
  play-in     games after the last dense date (>=5 games) and before the first
              playoff game; 2020 onward only
  tournament  a solitary December game, 2023-24 onward (NBA Cup final)

    python3 relabel_seasons.py plan        # what would change, no writes
    python3 relabel_seasons.py fetch       # pull playoff indexes (~29 requests)
    python3 relabel_seasons.py relabel     # rewrite archived games.parquet
    python3 relabel_seasons.py reexport    # rebuild season aggregate files

After reexport, the per-player files still carry stale labels:

    python3 export_r2.py players

Requires: boto3, duckdb, requests, beautifulsoup4
"""

import argparse
import gzip
import os
import re
import sys
import tempfile
import time
from datetime import timedelta

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
PO_DIR = os.path.join(HOME, "raw", "playoff_index")
ARCHIVE = "v2/archive"
EXPORT = "v2"

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}


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
                        region_name="auto", config=Config(retries={"max_attempts": 3}))


def archived_seasons(s3, bucket):
    seasons, token = set(), None
    while True:
        kw = {"Bucket": bucket, "Prefix": ARCHIVE + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) >= 3:
                seasons.add(parts[2])
        if not page.get("IsTruncated"):
            return sorted(seasons)
        token = page.get("NextContinuationToken")


# ---------------------------------------------------------------------------

def playoff_slugs(season, cache_only=False):
    """BBRef's playoff index for a season -> set of box-score slugs."""
    from bs4 import BeautifulSoup
    end_year = int(season.split("-")[0]) + 1
    os.makedirs(PO_DIR, exist_ok=True)
    path = os.path.join(PO_DIR, "NBA_%d_playoffs.html.gz" % end_year)

    if os.path.exists(path):
        html = gzip.open(path, "rt", encoding="utf-8").read()
    elif cache_only:
        return None
    else:
        import requests
        url = ("https://www.basketball-reference.com/playoffs/NBA_%d_games.html"
               % end_year)
        r = requests.get(url, headers=UA, timeout=60)
        r.encoding = "utf-8"
        time.sleep(5.5)
        if r.status_code != 200:
            print("    %s: playoff index HTTP %s" % (season, r.status_code))
            return set()
        html = r.text
        gzip.open(path, "wt", encoding="utf-8").write(html)

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="schedule")
    if not table:
        return set()
    out = set()
    for row in (table.find("tbody") or table).find_all("tr"):
        cell = row.find(attrs={"data-stat": "box_score_text"})
        a = cell.find("a") if cell else None
        if a and a.get("href"):
            m = re.search(r"/boxscores/(\d{9}[A-Z]{3})\.html", a["href"])
            if m:
                out.add(m.group(1))
    return out


def classify(con, season, slugs):
    """Return {game_id: season_type} for one season's games table (already loaded
    as a DuckDB view named g)."""
    rows = con.execute("SELECT game_id, game_date, source_game_id FROM g").fetchall()
    by_date = {}
    for gid, d, slug in rows:
        by_date.setdefault(d, []).append((gid, slug))

    out = {}
    po_dates = []
    for d, items in by_date.items():
        for gid, slug in items:
            if slug in slugs:
                out[gid] = "playoff"
                po_dates.append(d)
    first_po = min(po_dates) if po_dates else None

    end_year = int(season.split("-")[0]) + 1

    # play-in: after the last dense (>=5 games) date, before the playoffs
    last_dense = None
    if first_po and end_year >= 2020:
        dense = [d for d, items in by_date.items() if len(items) >= 5 and d < first_po]
        last_dense = max(dense) if dense else None

    for d, items in by_date.items():
        for gid, slug in items:
            if gid in out:
                continue
            if last_dense and last_dense < d < first_po:
                out[gid] = "playin"
            else:
                out[gid] = "regular"

    # NBA Cup final: a solitary December game, 2023-24 onward
    if end_year >= 2024:
        for d, items in by_date.items():
            if d.month == 12 and len(items) == 1:
                gid = items[0][0]
                if out.get(gid) == "regular":
                    out[gid] = "tournament"
    return out


def counts(mapping):
    c = {}
    for v in mapping.values():
        c[v] = c.get(v, 0) + 1
    return c


# ---------------------------------------------------------------------------

def run(cmd, env):
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    seasons = archived_seasons(s3, bucket)
    if not seasons:
        sys.exit("  no archived seasons found under %s/" % ARCHIVE)

    if cmd == "fetch":
        for s in seasons:
            got = playoff_slugs(s)
            print("  %-9s %d playoff slugs" % (s, len(got) if got else 0))
        return

    con = duckdb.connect()
    changed_total = 0

    with tempfile.TemporaryDirectory() as tmp:
        for season in seasons:
            gkey = "%s/%s/games.parquet" % (ARCHIVE, season)
            gpath = os.path.join(tmp, "games.parquet")
            try:
                s3.download_file(bucket, gkey, gpath)
            except Exception:
                print("  %-9s no games.parquet, skipped" % season)
                continue

            con.execute("CREATE OR REPLACE VIEW g AS SELECT * FROM read_parquet('%s')" % gpath)
            before = dict(con.execute(
                "SELECT season_type, COUNT(*) FROM g GROUP BY 1").fetchall())

            slugs = playoff_slugs(season, cache_only=True)
            if slugs is None:
                print("  %-9s playoff index not cached — run 'fetch' first" % season)
                continue

            mapping = classify(con, season, slugs)
            after = counts(mapping)
            changed = sum(1 for gid, t in mapping.items()
                          if t != con.execute(
                              "SELECT season_type FROM g WHERE game_id=?", [gid]
                          ).fetchone()[0])

            print("  %-9s before %-34s after %s%s"
                  % (season,
                     " ".join("%s=%d" % kv for kv in sorted(before.items())),
                     " ".join("%s=%d" % kv for kv in sorted(after.items())),
                     "" if changed == 0 else "   (%d changed)" % changed))
            changed_total += changed

            if cmd == "plan" or changed == 0:
                continue

            # rewrite games.parquet with corrected labels
            mp = os.path.join(tmp, "map.parquet")
            con.execute("DROP TABLE IF EXISTS m")
            con.execute("CREATE TABLE m (game_id VARCHAR, st VARCHAR)")
            con.executemany("INSERT INTO m VALUES (?,?)", list(mapping.items()))
            out = os.path.join(tmp, "games_new.parquet")
            con.execute("""
                COPY (SELECT g.* REPLACE (m.st AS season_type)
                      FROM g JOIN m ON m.game_id = g.game_id)
                TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % out)
            n_new = con.execute(
                "SELECT COUNT(*) FROM read_parquet('%s')" % out).fetchone()[0]
            n_old = con.execute("SELECT COUNT(*) FROM g").fetchone()[0]
            if n_new != n_old:
                sys.exit("  %s: row loss %d -> %d, aborting" % (season, n_old, n_new))
            s3.upload_file(out, bucket, gkey)

            if cmd == "reexport":
                reexport_season(con, s3, bucket, season, tmp, out)

    print("\n  %d games relabelled" % changed_total)
    if cmd == "reexport":
        print("  per-player files still carry old labels:")
        print("    python3 export_r2.py players")
    elif cmd == "relabel" and changed_total:
        print("  next: python3 relabel_seasons.py reexport")


def reexport_season(con, s3, bucket, season, tmp, games_path):
    """Rebuild the season aggregate files from corrected games + archived box."""
    bkey = "%s/%s/player_game_box.parquet" % (ARCHIVE, season)
    bpath = os.path.join(tmp, "box.parquet")
    try:
        s3.download_file(bucket, bkey, bpath)
    except Exception:
        print("      %s: no box parquet, aggregates not rebuilt" % season)
        return

    # registry gives full_name and birthdate for the age column
    rkey = "%s/registry/players.parquet" % EXPORT
    rpath = os.path.join(tmp, "reg.parquet")
    s3.download_file(bucket, rkey, rpath)

    ps = os.path.join(tmp, "ps.parquet")
    con.execute("""
        COPY (
          SELECT b.player_id, p.full_name, g.season, g.league, g.season_type,
                 COUNT(DISTINCT b.team_abbr) AS teams, MAX(b.team_abbr) AS last_team,
                 COUNT(*) AS gp,
                 SUM(CASE WHEN b.started THEN 1 ELSE 0 END) AS gs,
                 SUM(b.seconds_played)/60.0 AS mp,
                 SUM(b.fgm) fgm, SUM(b.fga) fga, SUM(b.fg3m) fg3m, SUM(b.fg3a) fg3a,
                 SUM(b.ftm) ftm, SUM(b.fta) fta, SUM(b.orb) orb, SUM(b.drb) drb,
                 SUM(b.trb) trb, SUM(b.ast) ast, SUM(b.stl) stl, SUM(b.blk) blk,
                 SUM(b.tov) tov, SUM(b.pf) pf, SUM(b.pts) pts,
                 SUM(b.plus_minus) plus_minus,
                 AVG(date_diff('day', p.birthdate, g.game_date)/365.25) AS age,
                 MIN(g.game_date) first_game, MAX(g.game_date) last_game
          FROM read_parquet('%s') b
          JOIN read_parquet('%s') g ON g.game_id = b.game_id
          JOIN read_parquet('%s') p ON p.player_id = b.player_id
          WHERE b.played
          GROUP BY 1,2,3,4,5
        ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (bpath, games_path, rpath, ps))
    s3.upload_file(ps, bucket, "%s/seasons/%s/player_season.parquet" % (EXPORT, season))

    ts = os.path.join(tmp, "ts.parquet")
    con.execute("""
        COPY (
          SELECT g.season, g.league, g.season_type, b.team_abbr,
                 COUNT(DISTINCT g.game_id) gp,
                 SUM(b.fga) fga, SUM(b.fta) fta, SUM(b.orb) orb, SUM(b.tov) tov,
                 SUM(b.fgm) fgm, SUM(b.fg3m) fg3m, SUM(b.ftm) ftm,
                 SUM(b.pts) pts, SUM(b.trb) trb, SUM(b.ast) ast,
                 SUM(b.fga) + 0.44*SUM(b.fta) - SUM(b.orb) + SUM(b.tov) AS poss_est
          FROM read_parquet('%s') b
          JOIN read_parquet('%s') g ON g.game_id = b.game_id
          WHERE b.played GROUP BY 1,2,3,4
        ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (bpath, games_path, ts))
    s3.upload_file(ts, bucket, "%s/seasons/%s/team_season.parquet" % (EXPORT, season))
    print("      %s aggregates rebuilt" % season)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "fetch", "relabel", "reexport"])
    a = ap.parse_args()
    run(a.cmd, load_env())
