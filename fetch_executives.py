#!/usr/bin/env python3
"""
fetch_executives.py — the one column the franchise pages do not carry.

Finish, playoff result and head coach all came from the franchise table in
thirty requests. The executive of record does not appear there — it sits in the
header of each individual team-season page, so this is one request per
team-season, about 890 of them.

At Basketball Reference's rate limit that is roughly 80 minutes. The job is
resumable: pages are cached on disk and anything already fetched is skipped, so
stopping and restarting costs nothing.

    python3 fetch_executives.py probe     # one page, show what parsed
    python3 fetch_executives.py fetch     # all team-seasons
    python3 fetch_executives.py merge     # fold into the history parquet
    python3 fetch_executives.py status

The merge rewrites v2/history/franchise.parquet with the executive column
filled, so the History tab picks it up with no frontend change.
"""

import argparse
import gzip
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
CACHE = os.path.join(HOME, "raw", "team_seasons")
OUT = os.path.join(HOME, "data", "executives.json")
KEY = "v2/history/franchise.parquet"
DELAY = 5.5

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
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def end_year(season):
    return int(season.split("-")[0]) + 1


def page_path(abbr, season):
    return os.path.join(CACHE, "%s-%s.html.gz" % (abbr, season))


def get_page(abbr, season, session=None):
    os.makedirs(CACHE, exist_ok=True)
    p = page_path(abbr, season)
    if os.path.exists(p):
        return gzip.open(p, "rt", encoding="utf-8").read(), True
    import requests
    s = session or requests.Session()
    url = "https://www.basketball-reference.com/teams/%s/%d.html" % (abbr, end_year(season))
    r = s.get(url, headers=UA, timeout=60)
    r.encoding = "utf-8"
    time.sleep(DELAY)
    if r.status_code != 200:
        return None, False
    gzip.open(p, "wt", encoding="utf-8").write(r.text)
    return r.text, False


EXEC_RE = re.compile(r"Executive:\s*</strong>\s*(?:<a[^>]*>)?([^<]+)", re.I)
EXEC_PLAIN = re.compile(r"Executive:\s*([A-Z][^<\n|]{2,40})", re.I)


def parse_exec(html):
    """The executive sits in the page header as 'Executive: Name', sometimes
    wrapped in a link and sometimes not."""
    if not html:
        return None
    m = EXEC_RE.search(html)
    if m:
        return m.group(1).strip().strip(",")
    # fall back to the text form, after stripping tags from the header block
    head = html[:40000]
    flat = re.sub(r"<[^>]+>", " ", head)
    m = EXEC_PLAIN.search(flat)
    return m.group(1).strip().strip(",") if m else None


def team_seasons(env):
    """Every (abbr, season) that played, from the exported team files."""
    import duckdb, tempfile
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    seasons, token = set(), None
    while True:
        kw = {"Bucket": bucket, "Prefix": "v2/seasons/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            p = o["Key"].split("/")
            if len(p) >= 3:
                seasons.add(p[2])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    out = []
    for s in sorted(seasons, reverse=True):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            p = f.name
        try:
            s3.download_file(bucket, "v2/seasons/%s/team_season.parquet" % s, p)
            for (a,) in con.execute(
                    "SELECT DISTINCT team_abbr FROM read_parquet('%s') "
                    "WHERE season_type='regular' ORDER BY 1" % p).fetchall():
                out.append((a, s))
        except Exception:
            pass
        finally:
            if os.path.exists(p):
                os.unlink(p)
    con.close()
    return out


def load_done():
    if os.path.exists(OUT):
        return json.load(open(OUT, encoding="utf-8"))
    return {}


def save_done(d):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(d, open(OUT, "w", encoding="utf-8"), indent=0, sort_keys=True)


def cmd_probe(env):
    for abbr, season in (("SAS", "2013-14"), ("BOS", "2007-08"), ("SEA", "1996-97")):
        html, cached = get_page(abbr, season)
        name = parse_exec(html)
        print("  %-4s %-8s %-10s %s"
              % (abbr, season, "cached" if cached else "fetched", name or "NOT FOUND"))
    print("\n  if those look right: python3 fetch_executives.py fetch")


def cmd_fetch(env):
    import requests
    pairs = team_seasons(env)
    done = load_done()
    todo = [(a, s) for a, s in pairs if "%s|%s" % (a, s) not in done]
    print("  %d team-seasons, %d already done, %d to fetch (~%.0f min)"
          % (len(pairs), len(pairs) - len(todo), len(todo), len(todo) * DELAY / 60))
    if not todo:
        return

    session = requests.Session()
    got = miss = 0
    for i, (abbr, season) in enumerate(todo, 1):
        html, cached = get_page(abbr, season, session)
        name = parse_exec(html)
        done["%s|%s" % (abbr, season)] = name
        if name:
            got += 1
        else:
            miss += 1
        if i % 25 == 0:
            save_done(done)
            print("  %4d/%d  found=%d missing=%d" % (i, len(todo), got, miss))
    save_done(done)
    print("\n  %d executives found, %d pages without one" % (got, miss))
    print("  next: python3 fetch_executives.py merge")


def cmd_merge(env):
    import duckdb, tempfile
    done = load_done()
    if not done:
        sys.exit("  nothing fetched yet")
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "f.parquet")
        s3.download_file(bucket, KEY, src)
        rows = os.path.join(tmp, "e.json")
        with open(rows, "w", encoding="utf-8") as f:
            for k, v in done.items():
                abbr, season = k.split("|")
                f.write(json.dumps({"team_abbr": abbr, "season": season,
                                    "exec_name": v}) + "\n")
        out = os.path.join(tmp, "out.parquet")
        con = duckdb.connect()
        con.execute("""
            COPY (SELECT h.* REPLACE (COALESCE(e.exec_name, h.executive) AS executive)
                  FROM read_parquet('%s') h
                  LEFT JOIN read_json_auto('%s') e
                    ON e.team_abbr = h.team_abbr AND e.season = h.season)
            TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (src, rows, out))
        n = con.execute("SELECT COUNT(*) FROM read_parquet('%s') WHERE executive <> ''"
                        % out).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM read_parquet('%s')" % out).fetchone()[0]
        con.close()
        s3.upload_file(out, bucket, KEY)
        print("  %d of %d history rows now carry an executive" % (n, total))


def cmd_status(env):
    done = load_done()
    have = sum(1 for v in done.values() if v)
    print("  %d pages parsed, %d with an executive" % (len(done), have))
    if have:
        from collections import Counter
        c = Counter(v for v in done.values() if v)
        print("\n  most common:")
        for name, n in c.most_common(8):
            print("    %-28s %d seasons" % (name, n))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "fetch", "merge", "status"])
    a = ap.parse_args()
    e = load_env()
    {"probe": cmd_probe, "fetch": cmd_fetch, "merge": cmd_merge,
     "status": cmd_status}[a.cmd](e)
