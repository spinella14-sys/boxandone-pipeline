#!/usr/bin/env python3
"""
fetch_logos.py — pull year-specific team logos into R2.

Basketball Reference shows the logo a franchise actually wore in a given
season, which is what makes a 1996-97 page feel like 1996-97. The logos are
served from their CDN under a versioned path, so rather than hardcode a URL
that will rot, this scrapes each team-season page and reads the <img> out of
the page itself.

Stored at v2/logos/{abbr}-{season}.png so the app can ask for the logo a team
wore that year. Hotlinking would work too, but storing means the app does not
break when their CDN path changes, and it keeps every request on one origin.

    python3 fetch_logos.py probe                  # find the pattern, 1 request
    python3 fetch_logos.py fetch                  # all team-seasons
    python3 fetch_logos.py fetch --season 2025-26
    python3 fetch_logos.py status

Rate limited to BBRef's floor. ~900 team-seasons, so about 75 minutes for the
full run; it skips anything already in R2, so it is resumable.
"""

import argparse
import os
import re
import sys
import time

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
CACHE = os.path.join(HOME, "raw", "logos")
PREFIX = "v2/logos"
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
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto", config=Config(retries={"max_attempts": 3}))


def end_year(season):
    return int(season.split("-")[0]) + 1


def team_seasons(env):
    """Every (abbr, season) that actually played, from the exported team files."""
    import duckdb, tempfile
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    out = []
    token = None
    seasons = set()
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

    # A season with only preseason games so far has no regular-season rows.
    # Rather than skip it, borrow the most recent regular-season team list.
    last_regular, pending = None, []
    for s in sorted(seasons, reverse=True):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            p = f.name
        try:
            s3.download_file(bucket, "v2/seasons/%s/team_season.parquet" % s, p)
            teams = [a for (a,) in con.execute(
                    "SELECT DISTINCT team_abbr FROM read_parquet('%s') "
                    "WHERE season_type='regular' ORDER BY 1" % p).fetchall()]
            if teams and last_regular is None:
                last_regular = teams
            elif s == max(seasons) and last_regular is None:
                pending.append(s)
            for a in teams:
                out.append((a, s))
        except Exception:
            pass
        finally:
            if os.path.exists(p):
                os.unlink(p)
    con.close()
    for s in pending:
        if last_regular:
            print("  %s has no regular-season games yet; using %d teams from "
                  "the latest regular season" % (s, len(last_regular)))
            out.extend((a, s) for a in last_regular)
    return out


def logo_url_from_page(abbr, season, session):
    """Read the team logo off the team-season page.

    <img class="teamlogo"> is the one we want. Matching loosely on "/logos/"
    picks up Basketball Reference's own header wordmark, which appears first in
    the document — that is what produced 892 copies of the Sports Reference
    logo on the previous run."""
    from bs4 import BeautifulSoup
    url = "https://www.basketball-reference.com/teams/%s/%d.html" % (abbr, end_year(season))
    r = session.get(url, headers=UA, timeout=60)
    if r.status_code != 200:
        return None, r.status_code
    soup = BeautifulSoup(r.text, "html.parser")

    img = soup.find("img", class_="teamlogo")
    if img is None:
        for cand in soup.find_all("img"):
            src = cand.get("src") or ""
            if "/tlogo/" in src:
                img = cand
                break
    if img is None:
        return None, 200

    src = img.get("src") or ""
    if not src:
        return None, 200
    if src.startswith("//"):
        src = "https:" + src
    return src, 200


def cmd_probe(env):
    import requests
    s = requests.Session()
    for abbr, season in [("DEN", "2024-25"), ("SEA", "1996-97"), ("VAN", "1996-97")]:
        url, code = logo_url_from_page(abbr, season, s)
        print("  %-5s %-8s HTTP %s  %s" % (abbr, season, code, url or "no logo img found"))
        time.sleep(DELAY)
    print("\n  if those resolve, run: python3 fetch_logos.py fetch")


def cmd_fetch(env, only=None, limit=None):
    import requests
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    os.makedirs(CACHE, exist_ok=True)

    have = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            have.add(o["Key"].split("/")[-1])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    print("  %d logos already in R2" % len(have))

    pairs = team_seasons(env)
    if only:
        pairs = [p for p in pairs if p[1] == only]
    todo = [(a, s) for a, s in pairs if "%s-%s.png" % (a, s) not in have]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("  nothing to fetch")
        return

    print("  %d team-seasons to fetch (~%.0f min)\n" % (todo and len(todo),
                                                        len(todo) * DELAY / 60))
    session = requests.Session()
    got = miss = 0
    for i, (abbr, season) in enumerate(todo, 1):
        local = os.path.join(CACHE, "%s-%s.png" % (abbr, season))
        if not os.path.exists(local):
            url, code = logo_url_from_page(abbr, season, session)
            time.sleep(DELAY)
            if not url:
                print("    %-5s %-8s no logo (HTTP %s)" % (abbr, season, code))
                miss += 1
                continue
            img = session.get(url, headers=UA, timeout=60)
            if img.status_code != 200 or len(img.content) < 200:
                print("    %-5s %-8s image HTTP %s" % (abbr, season, img.status_code))
                miss += 1
                continue
            open(local, "wb").write(img.content)
        s3.upload_file(local, bucket, "%s/%s-%s.png" % (PREFIX, abbr, season),
                       ExtraArgs={"ContentType": "image/png"})
        got += 1
        if i % 25 == 0:
            print("  %4d/%d  ok=%d miss=%d" % (i, len(todo), got, miss))
    print("\n  stored %d, missed %d" % (got, miss))


def cmd_status(env):
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    have, size, token = {}, 0, None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            n = o["Key"].split("/")[-1]
            m = re.match(r"([A-Z]{3})-(\d{4}-\d{2})\.png", n)
            if m:
                have.setdefault(m.group(2), []).append(m.group(1))
            size += o["Size"]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    for s in sorted(have, reverse=True):
        print("  %-9s %2d logos" % (s, len(have[s])))
    print("\n  %d seasons, %.1f MB" % (len(have), size / 1048576.0))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "fetch", "status"])
    ap.add_argument("--season")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    e = load_env()
    if a.cmd == "probe":
        cmd_probe(e)
    elif a.cmd == "fetch":
        cmd_fetch(e, a.season, a.limit)
    else:
        cmd_status(e)
