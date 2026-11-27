#!/usr/bin/env python3
"""
patch_gleague_host.py — run once.

stats.nba.com now ignores requests from Cloudflare's network (the relay's
/diag timed out on every one), but the G League's own stats site,
stats.gleague.nba.com — the same system under another name — answers through
the relay in under a second. On GitHub, G League requests now go there
(the relay's /gl-stats/ route). If that site doesn't offer a request (404),
the old stats.nba.com route is tried once. On the Mac nothing changes:
requests go straight to stats.nba.com, which answers the Mac fine.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_gleague.py")
src = open(PATH).read()
if "/gl-stats/" in src:
    print("  ingest_gleague.py already patched")
    sys.exit()

OLD = '''    q = urlencode(params)
    if use_relay(relay):
        url = "%s/stats/%s?%s" % (os.environ["NBA_RELAY_URL"].rstrip("/"), endpoint, q)
        headers = {"x-relay-token": os.environ.get("NBA_RELAY_TOKEN", "")}
    else:
        url = "https://stats.nba.com/stats/%s?%s" % (endpoint, q)
        headers = HEADERS
    for attempt in range(tries):'''
NEW = '''    q = urlencode(params)
    if use_relay(relay):
        # the G League's own stats site first: stats.nba.com ignores the relay
        base = os.environ["NBA_RELAY_URL"].rstrip("/")
        headers = {"x-relay-token": os.environ.get("NBA_RELAY_TOKEN", "")}
        for route in ("gl-stats", "stats"):
            got = _get(cr, "%s/%s/%s?%s" % (base, route, endpoint, q), headers, endpoint, tries)
            if got != 404:
                return got
        return None
    got = _get(cr, "https://stats.nba.com/stats/%s?%s" % (endpoint, q), HEADERS, endpoint, tries)
    return None if got == 404 else got


def _get(cr, url, headers, endpoint, tries):
    """JSON, None (no data / failed), or 404 (this route doesn't offer it)."""
    for attempt in range(tries):'''
if src.count(OLD) != 1:
    sys.exit("  ingest_gleague.py not as expected — nothing written")
src = src.replace(OLD, NEW)

OLD2 = '''        if r.status_code in (400, 404):
            _FAILED[0] = 0
            return None'''
NEW2 = '''        if r.status_code == 404:
            _FAILED[0] = 0
            return 404
        if r.status_code == 400:
            _FAILED[0] = 0
            return None'''
if src.count(OLD2) != 1:
    sys.exit("  ingest_gleague.py not as expected (404) — nothing written")
src = src.replace(OLD2, NEW2)
open(PATH, "w").write(src)
print("  ingest_gleague.py patched: on GitHub, G League requests go to stats.gleague.nba.com")
