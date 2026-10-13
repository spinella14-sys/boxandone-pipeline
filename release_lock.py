#!/usr/bin/env python3
"""
release_lock.py — free the database lock if THIS GitHub run holds it.

A run GitHub kills at its time limit never reaches its own cleanup, and the
lock it leaves makes every later run exit for two hours. The workflows call
this as a last step when a run is cancelled or fails. It deletes the lock only
when the lock names this run's id, so it can never free another run's lock.
"""
import json
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import nightly2 as N  # noqa: E402

env = N.load_env()
s3 = N.s3c(env)
bucket = env["R2_BUCKET_NAME"]
me = os.environ.get("GITHUB_RUN_ID")
try:
    held = json.loads(s3.get_object(Bucket=bucket, Key=N.LOCK_KEY)["Body"].read())
except Exception:
    print("no lock held")
    sys.exit(0)
if me and str(held.get("host")) == str(me):
    N.release_lock(s3, bucket)
    print("released lock held by this run (%s)" % me)
else:
    print("lock belongs to %s, not this run (%s) — left alone" % (held.get("host"), me))
