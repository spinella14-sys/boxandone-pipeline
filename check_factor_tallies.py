#!/usr/bin/env python3
"""
check_factor_tallies.py — read-only. Where do the per-stint box tallies drift
from the box score, and does it explain the weak 2019-20 factor fit?

For each season: every game's stint tallies summed per side against the NBA
box score, stat by stat; the worst games; what the play-by-play looks like in
the worst one; and the factor-to-points fit (R²) on all team-games versus only
those whose tallies match.

    python3 check_factor_tallies.py --seasons 2019-20,2024-25
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import cdn_feed as C  # noqa: E402

LINEUPS = os.path.join(HOME, "data", "lineups")
STATS = [("fga", "fieldGoalsAttempted"), ("fgm", "fieldGoalsMade"),
         ("fg3a", "threePointersAttempted"), ("fg3m", "threePointersMade"),
         ("fta", "freeThrowsAttempted"), ("ftm", "freeThrowsMade"),
         ("orb", "reboundsOffensive"), ("drb", "reboundsDefensive"),
         ("tov", "turnoversTotal"), ("ast", "assists")]


def r2_fit(rows):
    if len(rows) < 50:
        return None
    X = np.array([[1, r["efg"], r["tovr"], r["orbr"], r["ftr"]] for r in rows])
    y = np.array([r["ortg"] for r in rows])
    w = np.sqrt(np.array([r["poss"] for r in rows], dtype=float))
    b, *_ = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)
    res = w * (y - X @ b)
    tot = w * (y - np.average(y, weights=w ** 2))
    return 1 - (res ** 2).sum() / (tot ** 2).sum(), b


def season(s):
    import duckdb
    path = os.path.join(LINEUPS, "%s.parquet" % s)
    if not os.path.exists(path):
        print("  %s: no lineup file" % s)
        return
    keys = [k for k, _ in STATS]
    cols = ", ".join("SUM(%s_%s)" % (side, k) for side in ("home", "away") for k in keys)
    rows = duckdb.connect().execute(
        "SELECT game_id, SUM(home_pts), SUM(away_pts), SUM(home_poss), SUM(away_poss), %s "
        "FROM read_parquet(?) WHERE reliable GROUP BY 1" % cols, [path]).fetchall()
    n = len(keys)
    diff = defaultdict(list)          # stat -> [(abs diff, gid, side, ours, box)]
    worst = Counter()
    team_games = []
    for r in rows:
        gid = r[0]
        box = C.load(gid, "box", fetch=False)
        if not box:
            continue
        g = box["game"]
        ok = True
        for i, (side, key) in enumerate((("home", "homeTeam"), ("away", "awayTeam"))):
            st = g[key].get("statistics") or {}
            ours = dict(zip(keys, r[5 + i * n:5 + (i + 1) * n]))
            for k, b in STATS:
                bv = st.get(b)
                if bv is None and k == "tov":
                    bv = st.get("turnovers")
                if bv is None:
                    continue
                d = (ours[k] or 0) - bv
                if d:
                    diff[k].append((abs(d), gid, side, ours[k], bv))
                    worst[gid] += abs(d)
                    if k in ("fga", "fgm", "fg3a", "fg3m", "fta", "ftm", "tov", "orb"):
                        ok = False
        for i, side in enumerate(("home", "away")):
            o = dict(zip(keys, r[5 + i * n:5 + (i + 1) * n]))
            d = dict(zip(keys, r[5 + (1 - i) * n:5 + (2 - i) * n]))
            pts, poss = r[1 + i], r[3 + i]
            if not poss or poss < 40 or not o["fga"] or not (o["orb"] or 0) + (d["drb"] or 0):
                continue
            team_games.append({"ok": ok, "poss": poss, "ortg": 100.0 * pts / poss,
                               "efg": (o["fgm"] + 0.5 * o["fg3m"]) / o["fga"], "tovr": o["tov"] / poss,
                               "orbr": o["orb"] / (o["orb"] + d["drb"]), "ftr": o["ftm"] / o["fga"]})
    print("\n  %s: %d reliable games checked" % (s, len(rows)))
    for k, _ in STATS:
        v = diff[k]
        print("    %-4s  off in %4d games · mean miss %.1f · largest %s"
              % (k, len(set(x[1] for x in v)), np.mean([x[0] for x in v]) if v else 0,
                 "%s %s ours %s vs box %s" % max(v)[1:] if v else "-"))
    for label, sub in (("all team-games", team_games), ("tallies match", [t for t in team_games if t["ok"]]),
                       ("tallies off", [t for t in team_games if not t["ok"]])):
        f = r2_fit(sub)
        if f:
            print("    fit on %-15s n=%-5d R² %.2f · eFG %+.0f TOV %+.0f OREB %+.0f FT %+.0f"
                  % (label, len(sub), f[0], f[1][1], f[1][2], f[1][3], f[1][4]))
    if worst:
        gid = worst.most_common(1)[0][0]
        pbp = C.load(gid, "pbp", fetch=False) or {}
        acts = (pbp.get("game") or {}).get("actions") or []
        nums = Counter(a.get("actionNumber") for a in acts)
        print("    worst game %s: %d actions, %d action numbers repeated" %
              (gid, len(acts), sum(1 for c in nums.values() if c > 1)))
        kinds = Counter(((a.get("actionType") or "").lower(), (a.get("subType") or "").lower()[:14],
                         a.get("shotResult") or "", bool(a.get("teamId")))
                        for a in acts if (a.get("actionType") or "").lower()
                        in ("2pt", "3pt", "freethrow", "rebound", "turnover"))
        for (t, st, res, has_team), c in sorted(kinds.items()):
            print("      %-9s %-14s %-7s team=%s  %d" % (t, st, res, has_team, c))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2019-20,2024-25")
    for s in ap.parse_args().seasons.split(","):
        season(s.strip())


if __name__ == "__main__":
    main()
