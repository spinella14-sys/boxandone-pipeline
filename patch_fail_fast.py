#!/usr/bin/env python3
"""
patch_fail_fast.py — run once.

Since Saturday afternoon the relay has answered stats.nba.com requests with
HTTP 520. Each G League request then spent minutes retrying, so:
  - the hourly G League step took ~14 minutes instead of seconds, and
  - the nightly, with ~35 such requests, ran past GitHub's 90-minute limit and
    was killed before it could write a log — which is why there are no nightly
    logs since Friday, and why the hourly runs queued behind it were cancelled.

Now:
  - ingest_gleague.py: after two requests in a row fail, the rest of that run's
    NBA.com requests are skipped at once (said once in the log); retries are
    shorter and a request gives up after 30 seconds
  - nightly2.py: the G League steps and the two-way step get a hard limit
    (20 minutes each), so no one source can take the whole night down
  - hourly.py: the G League step gets a 10-minute limit
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected (%r) — nothing written" % (name, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


patch("ingest_gleague.py", [
    ('''def stats_get(endpoint, params, relay=False, tries=4):
    """One stats.nba.com call, directly or through the Worker. JSON or None."""
    from urllib.parse import urlencode
    from curl_cffi import requests as cr
    q = urlencode(params)''',
     '''_FAILED = [0]      # requests in a row that came back with nothing


def stats_get(endpoint, params, relay=False, tries=3):
    """One stats.nba.com call, directly or through the Worker. JSON or None.
    After two requests in a row fail, the rest of the run's requests are
    skipped: a source that is down should cost a minute, not the night."""
    from urllib.parse import urlencode
    from curl_cffi import requests as cr
    if _FAILED[0] >= 2:
        if _FAILED[0] == 2:
            print("      NBA.com is not answering — skipping the rest of this run's requests")
            _FAILED[0] += 1
        return None
    q = urlencode(params)'''),
    ('''    for attempt in range(tries):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=60)
        except Exception as e:
            print("      %s: %s" % (endpoint, str(e)[:70]))
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code in (400, 404):
            return None
        print("      %s: HTTP %s" % (endpoint, r.status_code))
        time.sleep(10 * (attempt + 1))
    return None''',
     '''    for attempt in range(tries):
        try:
            r = cr.get(url, headers=headers, impersonate="chrome", timeout=30)
        except Exception as e:
            print("      %s: %s" % (endpoint, str(e)[:70]))
            time.sleep(4 * (attempt + 1))
            continue
        if r.status_code == 200:
            _FAILED[0] = 0
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code in (400, 404):
            _FAILED[0] = 0
            return None
        print("      %s: HTTP %s" % (endpoint, r.status_code))
        time.sleep(5 * (attempt + 1))
    _FAILED[0] += 1
    return None'''),
], "_FAILED")

patch("nightly2.py", [
    ('''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season])''',
     '''        ok &= run(log, ["ingest_gleague.py", "run", "--season", season], timeout=1200)'''),
    ('''        if not run(log, ["ingest_gleague.py", "teams", "--season", season]):''',
     '''        if not run(log, ["ingest_gleague.py", "teams", "--season", season], timeout=1200):'''),
    ('''        if not run(log, ["ingest_twoway.py", "run", "--season", season]):''',
     '''        if not run(log, ["ingest_twoway.py", "run", "--season", season], timeout=1200):'''),
], 'season], timeout=1200)')

patch("hourly.py", [
    ('''            if not N.run(log, ["ingest_gleague.py", "run", "--season", season, "--only-new"]):''',
     '''            if not N.run(log, ["ingest_gleague.py", "run", "--season", season, "--only-new"], timeout=600):'''),
], '"--only-new"], timeout=600')
