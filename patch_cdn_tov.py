#!/usr/bin/env python3
"""
patch_cdn_tov.py — run once.

The CDN writes some turnover types hyphenated ("3-second-violation",
"offensive-kicked-ball", "jumpball violation"), so they missed the lookup and
came out as "3-second-violation" instead of stats.nba.com's "3 Second
Violation". Hyphens become spaces and a trailing "violation" or leading
"offensive" is dropped before the lookup, plus the jump-ball spelling.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "cdn_feed.py")
src = open(path).read()
if "_tov_key" in src:
    print("already patched")
    sys.exit(0)

old_fn = '''    return TOV.get(st) or _title(st) or "Unknown"'''
new_fn = '''    return TOV.get(st) or TOV.get(_tov_key(st)) or _title(st) or "Unknown"


def _tov_key(st):
    """'3-second-violation' -> '3 second', 'offensive-kicked-ball' -> 'kicked ball'."""
    k = " ".join(st.replace("-", " ").split())
    if k.startswith("offensive ") and k != "offensive foul" \\
            and k != "offensive goaltending":
        k = k[len("offensive "):]
    if k.endswith(" violation") and k not in ("lane violation", "jumpball violation"):
        k = k[: -len(" violation")]
    return k'''
old_map = '''    "kicked ball": "Kicked Ball Violation", "lane violation": "Lane Violation Turnover",'''
new_map = '''    "kicked ball": "Kicked Ball Violation", "lane violation": "Lane Violation Turnover",
    "jumpball violation": "Jump Ball Violation", "jump ball violation": "Jump Ball Violation",'''
for old, new in ((old_fn, new_fn), (old_map, new_map)):
    if src.count(old) != 1:
        sys.exit("expected text not found — nothing written")
    src = src.replace(old, new)
# the older entry would now shadow the corrected spelling
src = src.replace('''    "jump ball violation": "Jump Ball Violation Turnover",\n''', "")
open(path, "w").write(src)

sys.path.insert(0, os.path.dirname(path))
import cdn_feed as C  # noqa: E402
for raw in ("3-second-violation", "offensive-kicked-ball", "jumpball violation",
            "5-second-violation", "bad pass", "offensive foul", "traveling"):
    print("  %-24s -> %s" % (raw, C.tov_sub(raw, "")))
