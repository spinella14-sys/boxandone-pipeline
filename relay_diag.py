#!/usr/bin/env python3
"""
relay_diag.py — ask the relay's /diag what it can reach right now. Nothing is
written. Reads the relay's address and token from ~/boxandone/.env (or the
environment); asks for them if neither has them, without echoing the token.

    python3 relay_diag.py
"""
import getpass
import json
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
vals = {}
try:
    for line in open(os.path.join(HOME, ".env")):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            vals[k] = v.strip().strip('"').strip("'")
except OSError:
    pass
url = os.environ.get("NBA_RELAY_URL") or vals.get("NBA_RELAY_URL") or "https://nba-relay.spinella14.workers.dev"
token = os.environ.get("NBA_RELAY_TOKEN") or vals.get("NBA_RELAY_TOKEN") or getpass.getpass("  relay token: ")

from curl_cffi import requests as cr
print("  asking %s/diag … (up to 20 seconds)" % url.rstrip("/"))
r = cr.get(url.rstrip("/") + "/diag", headers={"x-relay-token": token}, timeout=60)
if r.status_code != 200:
    sys.exit("  relay answered HTTP %s: %s" % (r.status_code, r.text[:200]))
for name, res in json.loads(r.text).items():
    ok = res.get("status") == 200
    print("  %-4s %-48s %-6s %6s ms  %s" % ("OK" if ok else "--", name, res.get("status"), res.get("ms"),
          (res.get("start") or res.get("error") or "")[:60].replace("\n", " ")))
