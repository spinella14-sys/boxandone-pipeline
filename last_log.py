#!/usr/bin/env python3
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
