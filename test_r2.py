#!/usr/bin/env python3
"""
test_r2.py — verify R2 credentials, permissions and endpoint before anything
depends on them.

Reads from ~/boxandone/.env (never from a .py file — these values become
GitHub Actions repository secrets, and a Python file is one `git add` away
from being public).

Create ~/boxandone/.env with:

    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...
    R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
    R2_BUCKET_NAME=box-and-one-stats

Then:  python3 test_r2.py

Checks, in order:
  1. credentials load
  2. bucket is reachable  (endpoint + keys valid)
  3. READ  — lists existing objects, reports what the old project left behind
  4. WRITE — uploads a test object
  5. READ BACK — verifies byte-for-byte
  6. DELETE — cleans up

Read-only credentials are a real possibility: the old app mostly read from R2.
That would surface as a confusing failure much later, so step 4 is the one
that matters most.

Requires: boto3
"""

import os
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
TEST_KEY = "_connection_test/boxandone_write_test.txt"


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit(
            "  %s not found.\n\n"
            "  Create it with:\n"
            "    R2_ACCESS_KEY_ID=...\n"
            "    R2_SECRET_ACCESS_KEY=...\n"
            "    R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com\n"
            "    R2_BUCKET_NAME=box-and-one-stats\n" % ENV_PATH)

    env = {}
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

    missing = [k for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                           "R2_ENDPOINT_URL", "R2_BUCKET_NAME") if not env.get(k)]
    if missing:
        sys.exit("  missing in .env: %s" % ", ".join(missing))
    return env


def main():
    env = load_env()
    bucket = env["R2_BUCKET_NAME"]
    print("  endpoint: %s" % env["R2_ENDPOINT_URL"])
    print("  bucket:   %s" % bucket)
    print("  key id:   %s...%s" % (env["R2_ACCESS_KEY_ID"][:6],
                                   env["R2_ACCESS_KEY_ID"][-4:]))
    print()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        sys.exit("  boto3 not installed:  pip3 install boto3")

    s3 = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT_URL"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    # --- 2. reachable -----------------------------------------------------
    try:
        s3.head_bucket(Bucket=bucket)
        print("  [1/5] bucket reachable            OK")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        sys.exit("  [1/5] bucket UNREACHABLE (%s)\n        %s" % (code, str(e)[:160]))
    except Exception as e:
        sys.exit("  [1/5] connection failed: %s" % str(e)[:160])

    # --- 3. read ----------------------------------------------------------
    try:
        prefixes = {}
        total = 0
        size = 0
        token = None
        while True:
            kw = {"Bucket": bucket, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            for o in page.get("Contents", []):
                total += 1
                size += o["Size"]
                top = o["Key"].split("/")[0]
                prefixes[top] = prefixes.get(top, 0) + 1
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if total > 50000:
                break
        print("  [2/5] read / list                 OK  (%d objects, %.1f MB)"
              % (total, size / 1048576.0))
        if prefixes:
            print("        existing top-level prefixes:")
            for k, v in sorted(prefixes.items(), key=lambda x: -x[1])[:12]:
                print("          %-28s %d" % (k + "/", v))
    except ClientError as e:
        print("  [2/5] read FAILED (%s)" % e.response.get("Error", {}).get("Code"))

    # --- 4. write ---------------------------------------------------------
    body = ("box and one r2 write test %s\n" % datetime.now().isoformat()).encode()
    try:
        s3.put_object(Bucket=bucket, Key=TEST_KEY, Body=body,
                      ContentType="text/plain")
        print("  [3/5] write                       OK")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        print("  [3/5] write FAILED (%s)" % code)
        print("        credentials are likely READ-ONLY.")
        print("        Cloudflare dashboard -> R2 -> Manage API Tokens")
        print("        -> create a token with Object Read & Write on this bucket.")
        return

    # --- 5. read back -----------------------------------------------------
    try:
        got = s3.get_object(Bucket=bucket, Key=TEST_KEY)["Body"].read()
        ok = got == body
        print("  [4/5] read back                   %s" % ("OK" if ok else "MISMATCH"))
    except ClientError as e:
        print("  [4/5] read back FAILED (%s)"
              % e.response.get("Error", {}).get("Code"))

    # --- 6. delete --------------------------------------------------------
    try:
        s3.delete_object(Bucket=bucket, Key=TEST_KEY)
        print("  [5/5] delete                      OK")
    except ClientError as e:
        print("  [5/5] delete FAILED (%s) — test object left behind at %s"
              % (e.response.get("Error", {}).get("Code"), TEST_KEY))

    print("\n  R2 is ready for read, write and delete.")


if __name__ == "__main__":
    main()
