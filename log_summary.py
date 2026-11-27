#!/usr/bin/env python3
"""
log_summary.py — one line per recent nightly and hourly run, from R2, with
what each said about the G League. Nothing is written.

    python3 log_summary.py            # the last 3 days
    python3 log_summary.py 5          # the last 5 days
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
env = N.load_env()
s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]

for kind in ("nightly", "hourly"):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/logs/%s/" % kind):
        keys += [o["Key"] for o in page.get("Contents", []) if os.path.basename(o["Key"]) >= since]
    keys.sort()
    print("\n  %s — %d runs since %s (UTC)" % (kind.upper(), len(keys), since))
    for k in keys:
        name = os.path.basename(k)
        m = re.match(r"(\d{8})_(\d{6})_(\w+)\.txt", name)
        when = "%s %s:%s" % (m.group(1)[4:], m.group(2)[:2], m.group(2)[2:4]) if m else name
        status = m.group(3) if m else ""
        text = s3.get_object(Bucket=bucket, Key=k)["Body"].read().decode(errors="replace")
        lines = text.splitlines()
        gl = [l.strip() for l in lines if re.search(r"new G League game|G League step|2026-27 schedule|"
                                                     r"fetched \d+, failed|nothing to fetch|EXCEPTION|"
                                                     r"Traceback|Error|HTTP 4|HTTP 5|locked|stopped", l)]
        last = lines[-1].strip() if lines else ""
        took = re.match(r"\s*([\d.]+)s", last)
        print("    %s  %-7s %6ss  %s" % (when, status, took.group(1) if took else "?",
                                       " | ".join(re.sub(r"^\s*[\d.]+s\s+", "", g) for g in gl[:4])[:190]))
