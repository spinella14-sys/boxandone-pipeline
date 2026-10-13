#!/usr/bin/env python3
"""
compare_feeds.py — NBA's two play-by-play feeds, side by side. Mac only
(it needs stats.nba.com).

The pipeline reads stats.nba.com's playbyplayv3. GitHub can only reach
cdn.nba.com's liveData feed. Before switching, this lines the two up event by
event (actionNumber) for a handful of games and reports, for every field the
parser reads, how often they agree — plus how one feed's action types
translate into the other's. It also lists what the liveData box score carries,
for preseason box scores and the player id bridge.

    python3 compare_feeds.py              # writes feed_compare.txt
"""
import collections
import json
import os
import time

from curl_cffi import requests as cr

HEADERS = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
           "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
           "Accept": "application/json, text/plain, */*"}
GAMES = ["0022500001", "0022500450", "0022501100", "0042500101", "0042500406",
         "0012600001"]
FIELDS = ["period", "clock", "personId", "teamId", "teamTricode", "playerName",
          "actionType", "subType", "description", "shotResult", "shotValue",
          "location", "shotDistance", "xLegacy", "yLegacy", "isFieldGoal",
          "pointsTotal", "scoreHome", "scoreAway"]
OUT = []


def say(s=""):
    print(s)
    OUT.append(s)


def get(url):
    for i in range(3):
        try:
            r = cr.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                return r.json()
            return {"_status": r.status_code}
        except Exception as e:
            err = str(e)[:60]
            time.sleep(3)
    return {"_status": err}


def norm(v):
    # the two feeds disagree on types ("12" vs 12, 1 vs True); compare values
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 1)
    except (TypeError, ValueError):
        return str(v).strip()


agree = collections.defaultdict(lambda: [0, 0])
diffs = collections.defaultdict(list)
vocab = collections.Counter()
only_live = collections.Counter()
only_v3 = collections.Counter()
live_keys, v3_keys = set(), set()

for gid in GAMES:
    v3 = get("https://stats.nba.com/stats/playbyplayv3?GameID=%s&StartPeriod=0&EndPeriod=14" % gid)
    live = get("https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_%s.json" % gid)
    time.sleep(1)
    if "_status" in v3 or "_status" in live:
        say("%s  v3 %s  live %s" % (gid, v3.get("_status", "ok"), live.get("_status", "ok")))
        continue
    va = v3["game"]["actions"]
    la = live["game"]["actions"]
    for a in va:
        v3_keys.update(a)
    for a in la:
        live_keys.update(a)
    # v3 can hold several rows per actionNumber; keep them all
    vby = collections.defaultdict(list)
    for a in va:
        vby[a.get("actionNumber")].append(a)
    lby = {a.get("actionNumber"): a for a in la}
    both = [n for n in lby if n in vby]
    say("%s  v3 %d rows (%d actionNumbers)  live %d  matched %d"
        % (gid, len(va), len(vby), len(la), len(both)))
    for n in both:
        L, V = lby[n], vby[n][0]
        vocab[(L.get("actionType"), L.get("subType"), V.get("actionType"), V.get("subType"))] += 1
        for f in FIELDS:
            a, b = norm(L.get(f)), norm(V.get(f))
            agree[f][1] += 1
            if a == b:
                agree[f][0] += 1
            elif len(diffs[f]) < 4:
                diffs[f].append("%s#%s live=%r v3=%r" % (gid, n, L.get(f), V.get(f)))
    for n in lby:
        if n not in vby:
            only_live[(lby[n].get("actionType"), lby[n].get("subType"))] += 1
    for n in vby:
        if n not in lby:
            for a in vby[n]:
                only_v3[(a.get("actionType"), a.get("subType"))] += 1

say("\nFIELD AGREEMENT (same actionNumber)")
for f in FIELDS:
    ok, n = agree[f]
    say("  %-14s %6d/%-6d %5.1f%%   %s" % (f, ok, n, 100.0 * ok / max(n, 1),
                                         " | ".join(diffs[f][:2])))

say("\nACTION TYPES  live (type / subType)  ->  v3 (type / subType)   count")
for (lt, ls, vt, vs), c in vocab.most_common(60):
    say("  %-12s %-22s -> %-14s %-22s %5d" % (lt, ls, vt, vs, c))

say("\nIN LIVE ONLY: %s" % dict(only_live.most_common(15)))
say("IN V3 ONLY:   %s" % dict(only_v3.most_common(15)))
say("\nlive keys not in v3: %s" % sorted(live_keys - v3_keys))
say("v3 keys not in live: %s" % sorted(v3_keys - live_keys))

box = get("https://cdn.nba.com/static/json/liveData/boxscore/boxscore_%s.json" % GAMES[-1])
if "_status" not in box:
    g = box["game"]
    say("\nLIVE BOX SCORE (%s)" % GAMES[-1])
    say("  game keys: %s" % sorted(g))
    p = g["homeTeam"]["players"][0]
    say("  player keys: %s" % sorted(p))
    say("  statistics keys: %s" % sorted(p.get("statistics", {})))
    say("  example: %s" % json.dumps({k: p[k] for k in p if k != "statistics"})[:300])
else:
    say("\nlive box score %s: %s" % (GAMES[-1], box["_status"]))

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_compare.txt")
open(path, "w").write("\n".join(OUT) + "\n")
print("\nwritten to %s" % path)
