#!/usr/bin/env python3
"""
lineup_check.py — do the CDN feed's substitutions add up to real lineups?

stats.nba.com's play-by-play failed this test: players recorded steals while
the feed had them on the bench. Before building lineups, on/off and RAPM on
the CDN feed (2019-20 on), this walks a sample of games and checks three
things against each other:

  1. every event credited to a player (shot, free throw, rebound, assist,
     turnover, steal, block, personal foul) finds him on the floor
  2. each team has exactly five on the floor at every event
  3. minutes added up from the lineup timeline match the box score's

Period 1 starts from the box score's starters. Later periods are not listed
in the feed, so each is inferred: whoever acts, or is subbed out, before being
subbed in was on the floor when the period began. Technical fouls are left
out, since bench players draw them.

    python3 lineup_check.py                 # 5 games a season, 2019-20 to 2025-26
    python3 lineup_check.py --per-season 15
"""
import argparse
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import cdn_feed as C  # noqa: E402

SEASONS = ["2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
PLAYER_EVENTS = {"2pt", "3pt", "freethrow", "rebound", "turnover", "steal", "block", "foul", "jumpball"}


def secs(clock):
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def actors(a):
    """Player ids an action says were on the floor."""
    t = (a.get("actionType") or "").lower()
    out = []
    if t in PLAYER_EVENTS and a.get("personId"):
        if t == "foul" and "technical" in (a.get("subType") or "").lower():
            pass
        else:
            out.append(a["personId"])
    for k in ("assistPersonId", "stealPersonId", "blockPersonId"):
        if a.get(k):
            out.append(a[k])
    return out


def check_game(gid):
    pbp, box = C.load(gid, "pbp"), C.load(gid, "box")
    if not pbp or not box:
        return None
    g = box["game"]
    team_of, starters, box_min = {}, defaultdict(set), {}
    for side in ("homeTeam", "awayTeam"):
        tid = g[side]["teamId"]
        for p in g[side]["players"]:
            team_of[p["personId"]] = tid
            if str(p.get("starter")) == "1":
                starters[tid].add(p["personId"])
            box_min[p["personId"]] = (secs((p.get("statistics") or {}).get("minutes")) or 0) / 60.0

    acts = sorted(pbp["game"]["actions"], key=lambda a: (a.get("orderNumber") or 0))
    periods = sorted({a["period"] for a in acts if a.get("period")})
    res = Counter()
    examples = []
    played = defaultdict(float)

    for per in periods:
        pa = [a for a in acts if a.get("period") == per]
        length = 720 if per <= 4 else 300
        # who was on the floor when the period began
        if per == 1:
            on = defaultdict(set, {t: set(s) for t, s in starters.items()})
        else:
            on = defaultdict(set)
            subbed_in = set()
            for a in pa:
                t = (a.get("actionType") or "").lower()
                if t == "substitution":
                    pid, tid = a.get("personId"), a.get("teamId")
                    if (a.get("subType") or "").lower() == "in":
                        subbed_in.add(pid)
                    elif pid not in subbed_in and tid in team_of.values():
                        on[tid].add(pid)
                    continue
                for pid in actors(a):
                    tid = team_of.get(pid)
                    if tid and pid not in subbed_in and len(on[tid]) < 5:
                        on[tid].add(pid)
            for tid in set(team_of.values()):
                if len(on[tid]) != 5:
                    res["period_start_not_5"] += 1
        last = length
        for a in pa:
            now = secs(a.get("clock"))
            if now is not None and now <= last:
                for tid, s in on.items():
                    for pid in s:
                        played[pid] += (last - now) / 60.0
                last = now
            t = (a.get("actionType") or "").lower()
            if t == "substitution":
                pid, tid = a.get("personId"), a.get("teamId")
                if (a.get("subType") or "").lower() == "out":
                    if pid not in on[tid]:
                        res["sub_out_not_on_floor"] += 1
                    on[tid].discard(pid)
                else:
                    on[tid].add(pid)
                continue
            for pid in actors(a):
                tid = team_of.get(pid)
                if not tid:
                    continue
                res["events"] += 1
                if pid not in on[tid]:
                    res["event_off_floor"] += 1
                    if len(examples) < 3:
                        examples.append("P%s %s %s: %s" % (per, a.get("clock"), t, a.get("description")))
            for tid in set(team_of.values()):
                res["checks"] += 1
                if len(on[tid]) != 5:
                    res["not_5"] += 1
        if last > 0:
            for tid, s in on.items():
                for pid in s:
                    played[pid] += last / 60.0

    for pid, m in box_min.items():
        if m <= 0 and played.get(pid, 0) <= 0:
            continue
        res["players"] += 1
        if abs(played.get(pid, 0) - m) <= 0.5:
            res["minutes_match"] += 1
    return res, examples


def game_ids(per_season):
    import duckdb
    con = duckdb.connect(os.path.join(HOME, "data", "boxandone.duckdb"), read_only=True)
    rows = con.execute("""SELECT season, source_game_id FROM game_identifiers
                          WHERE source = 'nba' AND season IN (%s)""" %
                       ",".join("'%s'" % s for s in SEASONS)).fetchall()
    con.close()
    by = defaultdict(list)
    for s, g in rows:
        by[s].append(g)
    random.seed(11)
    out = []
    for s in SEASONS:
        reg = [g for g in by[s] if g[2] == "2"]
        po = [g for g in by[s] if g[2] == "4"]
        # seasons not bridged locally: NBA ids follow a fixed pattern
        # (002 + two-digit season + 0 + game number; 004... for playoffs)
        yy = s[2:4]
        if not reg:
            reg = ["002%s0%04d" % (yy, n) for n in range(1, 971)]
        if not po:
            po = ["004%s00%d%d%d" % (yy, r, sr, gm) for r, sr, gm in
                  ((1, 0, 1), (1, 1, 2), (1, 2, 3), (2, 0, 1), (3, 0, 2), (4, 0, 1))]
        out += [(s, g) for g in random.sample(reg, min(per_season, len(reg)))]
        out += [(s, g) for g in random.sample(po, min(1, len(po)))]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-season", type=int, default=5)
    a = ap.parse_args()
    games = game_ids(a.per_season)
    total = Counter()
    print("  %-11s %-8s %7s %9s %9s %9s %s" % ("game", "season", "events", "off-floor",
                                              "not five", "minutes", ""))
    bad_examples = []
    for season, gid in games:
        r = check_game(gid)
        time.sleep(0.2)
        if not r:
            print("  %-11s %-8s not on the CDN" % (gid, season))
            continue
        res, ex = r
        total.update(res)
        bad_examples += [gid + " " + e for e in ex]
        print("  %-11s %-8s %7d %9d %8.1f%% %4d/%-4d %s" % (
            gid, season, res["events"], res["event_off_floor"],
            100.0 * res["not_5"] / max(res["checks"], 1),
            res["minutes_match"], res["players"],
            "period start unclear x%d" % res["period_start_not_5"] if res["period_start_not_5"] else ""))
    print("\n  across %d games" % len(games))
    print("    events credited to a player off the floor: %d of %d (%.2f%%)"
          % (total["event_off_floor"], total["events"],
             100.0 * total["event_off_floor"] / max(total["events"], 1)))
    print("    moments without exactly five on the floor: %.2f%%"
          % (100.0 * total["not_5"] / max(total["checks"], 1)))
    print("    player minutes within 30 seconds of the box score: %d of %d (%.1f%%)"
          % (total["minutes_match"], total["players"],
             100.0 * total["minutes_match"] / max(total["players"], 1)))
    print("    substitutions out of a player not on the floor: %d" % total["sub_out_not_on_floor"])
    print("    team-periods whose starting five could not be inferred: %d" % total["period_start_not_5"])
    if bad_examples:
        print("\n  examples of off-floor events")
        for e in bad_examples[:12]:
            print("    " + e[:120])


if __name__ == "__main__":
    main()
