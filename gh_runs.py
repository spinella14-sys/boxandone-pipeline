#!/usr/bin/env python3
"""
gh_runs.py — what happened in recent GitHub runs, step by step, plus the
latest logs the jobs wrote to R2.

    python3 gh_runs.py            # last 8 runs
    python3 gh_runs.py --n 15
"""
import argparse
import json
import os
import urllib.request
from datetime import datetime

REPO = "spinella14-sys/boxandone-pipeline"


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "boxandone"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def mins(a, b):
    return "%5.1f min" % ((t(b) - t(a)).total_seconds() / 60) if a and b else "    ?    "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    n = ap.parse_args().n
    runs = get("https://api.github.com/repos/%s/actions/runs?per_page=%d" % (REPO, n))
    for r in runs["workflow_runs"]:
        print("\n%s  %-22s %-9s %s  (queued %s, ran %s)"
              % (r["created_at"], r["name"][:22], r["event"], r["conclusion"],
                 mins(r["created_at"], r.get("run_started_at")),
                 mins(r.get("run_started_at"), r["updated_at"])))
        for j in get(r["jobs_url"])["jobs"]:
            for s in j.get("steps") or []:
                print("    %-32s %-10s %s" % (s["name"][:32], s["conclusion"],
                                             mins(s.get("started_at"), s.get("completed_at"))))

    # the jobs' own logs, from R2
    env = {}
    p = os.path.expanduser("~/boxandone/.env")
    for line in open(p):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip('"').strip("'")
    import boto3
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name="auto")
    b = env["R2_BUCKET_NAME"]
    for prefix in ("v2/logs/hourly/", "v2/logs/nightly/"):
        keys = sorted(o["Key"] for o in
                      s3.list_objects_v2(Bucket=b, Prefix=prefix).get("Contents", []))
        print("\n%s  %d logs, latest: %s" % (prefix, len(keys), [k.split("/")[-1] for k in keys[-4:]]))
        if keys:
            body = s3.get_object(Bucket=b, Key=keys[-1])["Body"].read().decode()
            print("\n".join("    " + l for l in body.splitlines()[-25:]))
    try:
        lock = s3.get_object(Bucket=b, Key="v2/db/.nightly.lock")["Body"].read().decode()
        print("\nLOCK HELD: %s" % lock)
    except Exception:
        print("\nno lock held")


if __name__ == "__main__":
    main()
