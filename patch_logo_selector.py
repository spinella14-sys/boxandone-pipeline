#!/usr/bin/env python3
"""
patch_logo_selector.py — grab the team logo, not the site wordmark.

The selector matched `"tlogo" in src or "/logos/" in src` and took the first
hit. Basketball Reference's own header image is at /logos/bbr-logo.svg and
appears before the team logo in the document, so all 892 files are the Sports
Reference wordmark.

The team logo is unambiguous: <img class="teamlogo"> pointing at
/tlogo/bbr/{ABBR}-{YEAR}.png. Match the class, fall back to /tlogo/ in the
src, and never match /logos/.

Also clears the local cache and the stored objects, since every one of them is
the wrong image — a resumable scraper would otherwise skip them all.

    python3 patch_logo_selector.py          # patch + clear local cache
    python3 patch_logo_selector.py --purge  # also delete the 892 from R2

Then: python3 fetch_logos.py probe
      caffeinate -is python3 fetch_logos.py fetch
"""

import argparse
import ast
import os
import shutil
import sys

TARGET = os.path.expanduser("~/boxandone/fetch_logos.py")
CACHE = os.path.expanduser("~/boxandone/raw/logos")
ENV_PATH = os.path.expanduser("~/boxandone/.env")

NEW_FN = '''def logo_url_from_page(abbr, season, session):
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

'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge", action="store_true",
                    help="delete the bad objects from R2 as well")
    a = ap.parse_args()

    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    start = src.find("def logo_url_from_page(")
    end = src.find("def cmd_probe(")
    if start == -1 or end == -1:
        sys.exit("  could not locate logo_url_from_page()")
    src = src[:start] + NEW_FN + "\n" + src[end:]

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_sel", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  selector now targets img.teamlogo")

    if os.path.isdir(CACHE):
        n = len(os.listdir(CACHE))
        shutil.rmtree(CACHE)
        print("  cleared %d cached files (all the wrong image)" % n)

    if a.purge:
        env = {}
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        import boto3
        s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                          aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                          region_name="auto")
        bucket = env["R2_BUCKET_NAME"]
        keys, token = [], None
        while True:
            kw = {"Bucket": bucket, "Prefix": "v2/logos/", "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            s3.delete_objects(Bucket=bucket,
                              Delete={"Objects": [{"Key": k} for k in batch]})
        print("  deleted %d objects from R2" % len(keys))
    else:
        print("  R2 still holds the bad objects; the fetch overwrites them")

    print("\n  next: python3 fetch_logos.py probe")


if __name__ == "__main__":
    main()
