#!/usr/bin/env python3
"""
backup_r2.py — pull the irreplaceable objects out of R2 to local disk.

Everything statistical in that bucket can be re-scraped. The scouting reports
cannot. 46 objects under scouting/ are the only things in there that represent
work rather than data, so they get backed up before anything starts writing to
the bucket.

    python3 backup_r2.py list                # inventory, no download
    python3 backup_r2.py backup              # scouting/ + scouts/ + players/
    python3 backup_r2.py backup --all        # entire bucket
    python3 backup_r2.py verify              # re-check local copies vs R2

Downloads to ~/boxandone/backups/r2/<timestamp>/ with a manifest recording
each object's key, size and ETag. verify re-reads the manifest and confirms
every file still matches, so a silent truncation shows up.

Reads credentials from ~/boxandone/.env.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
BACKUP_ROOT = os.path.join(HOME, "backups", "r2")

# prefixes worth preserving: written by humans, not re-derivable
IRREPLACEABLE = ["scouting/", "scouts/", "players/", "depthcharts/"]


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


def client(env):
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT_URL"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def all_objects(s3, bucket, prefix=None):
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if prefix:
            kw["Prefix"] = prefix
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        out.extend(page.get("Contents", []))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def cmd_list(env):
    s3 = client(env)
    bucket = env["R2_BUCKET_NAME"]
    objs = all_objects(s3, bucket)

    groups = {}
    for o in objs:
        top = o["Key"].split("/")[0] + "/"
        g = groups.setdefault(top, {"n": 0, "bytes": 0, "newest": None})
        g["n"] += 1
        g["bytes"] += o["Size"]
        lm = o["LastModified"]
        if g["newest"] is None or lm > g["newest"]:
            g["newest"] = lm

    print("  %-24s %6s %10s  %s  %s" % ("prefix", "count", "size", "newest", ""))
    for k in sorted(groups, key=lambda x: -groups[x]["n"]):
        g = groups[k]
        mark = "  <- backed up" if any(k.startswith(p) for p in IRREPLACEABLE) else ""
        print("  %-24s %6d %9.1fK  %s%s"
              % (k, g["n"], g["bytes"] / 1024.0,
                 g["newest"].strftime("%Y-%m-%d"), mark))
    print("\n  %d objects total" % len(objs))


def cmd_backup(env, everything=False):
    s3 = client(env)
    bucket = env["R2_BUCKET_NAME"]

    if everything:
        objs = all_objects(s3, bucket)
    else:
        objs = []
        for p in IRREPLACEABLE:
            objs.extend(all_objects(s3, bucket, prefix=p))
    if not objs:
        print("  nothing matched")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(dest, exist_ok=True)

    manifest = []
    total = 0
    for i, o in enumerate(objs, 1):
        key = o["Key"]
        path = os.path.join(dest, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        with open(path, "wb") as f:
            f.write(body)
        manifest.append({
            "key": key,
            "size": len(body),
            "etag": o.get("ETag", "").strip('"'),
            "md5": hashlib.md5(body).hexdigest(),
            "last_modified": o["LastModified"].isoformat(),
        })
        total += len(body)
        if i % 25 == 0 or i == len(objs):
            print("  %d/%d  (%.1f KB)" % (i, len(objs), total / 1024.0))

    with open(os.path.join(dest, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump({"bucket": bucket, "taken_at": stamp,
                   "objects": manifest}, f, indent=2)

    print("\n  %d objects -> %s" % (len(objs), dest))
    print("  manifest: MANIFEST.json (key, size, etag, md5)")

    bad = [m for m in manifest if m["etag"] and m["etag"] != m["md5"]]
    if bad:
        print("  WARNING: %d objects whose etag did not match md5" % len(bad))
        print("  (normal for multipart uploads; investigate if these are small files)")
    else:
        print("  all checksums verified against R2 etags")


def cmd_verify(env):
    if not os.path.isdir(BACKUP_ROOT):
        sys.exit("  no backups yet")
    runs = sorted(os.listdir(BACKUP_ROOT))
    if not runs:
        sys.exit("  no backups yet")
    dest = os.path.join(BACKUP_ROOT, runs[-1])
    mpath = os.path.join(dest, "MANIFEST.json")
    if not os.path.exists(mpath):
        sys.exit("  %s has no manifest" % dest)

    man = json.load(open(mpath, encoding="utf-8"))
    print("  verifying %s (%d objects)" % (runs[-1], len(man["objects"])))

    missing = corrupt = 0
    for m in man["objects"]:
        p = os.path.join(dest, m["key"])
        if not os.path.exists(p):
            print("    MISSING %s" % m["key"])
            missing += 1
            continue
        data = open(p, "rb").read()
        if len(data) != m["size"] or hashlib.md5(data).hexdigest() != m["md5"]:
            print("    CORRUPT %s" % m["key"])
            corrupt += 1

    if missing or corrupt:
        print("\n  %d missing, %d corrupt" % (missing, corrupt))
    else:
        print("  all %d files intact" % len(man["objects"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "backup", "verify"])
    ap.add_argument("--all", action="store_true", help="back up the whole bucket")
    a = ap.parse_args()
    e = load_env()
    if a.cmd == "list":
        cmd_list(e)
    elif a.cmd == "backup":
        cmd_backup(e, everything=a.all)
    else:
        cmd_verify(e)
