#!/usr/bin/env python3
"""
patch_twoway_relay.py — run once (after updating the Cloudflare relay).

RealGM refuses GitHub's machines (HTTP 403 in the nightly log), so G League
transactions only updated when run from the Mac. On GitHub the page is now
asked for through the relay, the same Worker that carries stats.nba.com. On
the Mac it is fetched directly, as before.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "/realgm" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()
OLD = '''def fetch(url):
    from curl_cffi import requests as cr
    for attempt in range(3):
        try:
            r = cr.get(url, impersonate="chrome", timeout=40,
                       headers={"Accept": "text/html,application/xhtml+xml"})'''
NEW = '''def fetch(url):
    from curl_cffi import requests as cr
    headers = {"Accept": "text/html,application/xhtml+xml"}
    relay = os.environ.get("NBA_RELAY_URL")
    if not relay and os.environ.get("GITHUB_ACTIONS"):
        try:
            import nightly2 as N
            env = N.load_env()
            relay = env.get("NBA_RELAY_URL")
            os.environ.setdefault("NBA_RELAY_TOKEN", env.get("NBA_RELAY_TOKEN") or "")
        except Exception:
            relay = None
    if relay and os.environ.get("GITHUB_ACTIONS") and url.startswith("https://basketball.realgm.com/"):
        # RealGM refuses GitHub's machines: ask through the relay
        url = relay.rstrip("/") + "/realgm" + url[len("https://basketball.realgm.com"):]
        headers["x-relay-token"] = os.environ.get("NBA_RELAY_TOKEN", "")
    for attempt in range(3):
        try:
            r = cr.get(url, impersonate="chrome", timeout=40, headers=headers)'''
if src.count(OLD) != 1:
    sys.exit("  ingest_twoway.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  ingest_twoway.py patched: RealGM through the relay on GitHub")
