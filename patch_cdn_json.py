#!/usr/bin/env python3
"""
patch_cdn_json.py — run once. The CDN occasionally answers 200 with a body
that is not valid JSON (a truncated or garbled response), and that crashed
the lineup fetch. Now a bad body is retried; if it stays bad, the request
counts as a failure to retry another day, never as "this game does not exist".
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "cdn_feed.py")
src = open(path).read()
if "class FetchFailed" in src:
    print("already patched")
    sys.exit(0)
old_get = src[src.index("def _get(kind, gid):"):src.index("def _cache(gid, kind):")]
new_get = '''class FetchFailed(Exception):
    """The CDN could not be read just now; try again later."""


def _get(kind, gid):
    """JSON, or None when the game is not published (403/404). Raises
    FetchFailed when the CDN keeps failing or keeps sending a bad body."""
    from curl_cffi import requests as cr
    why = "no response"
    for attempt in range(4):
        try:
            r = cr.get(URL % (kind, kind, gid), headers=HEADERS,
                       impersonate="chrome", timeout=30)
        except Exception as e:
            why = str(e)[:80]
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (403, 404):      # game not published (yet)
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                why = "body is not valid JSON"
        else:
            why = "HTTP %s" % r.status_code
        time.sleep(5 * (attempt + 1))
    raise FetchFailed("%s %s: %s" % (kind, gid, why))


'''
src = src.replace(old_get, new_get)
old_ft = '''    pbp, box = load(gid, "pbp"), load(gid, "box")
    if not pbp or not box:
        return None
    acts = translate(pbp, box)'''
new_ft = '''    try:
        pbp, box = load(gid, "pbp"), load(gid, "box")
    except FetchFailed as e:
        print("    %s" % e)
        return None                        # counted as a failed fetch, retried next run
    if not pbp or not box:
        return None
    acts = translate(pbp, box)'''
if src.count(old_ft) != 1:
    sys.exit("fetch_translated not found as expected — nothing written")
src = src.replace(old_ft, new_ft)
open(path, "w").write(src)
print("cdn_feed.py patched — bad CDN responses are retried, never mistaken for a missing game")
