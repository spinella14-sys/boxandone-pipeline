#!/usr/bin/env python3
"""
check_six_factors.py — read-only. Does true shooting, turnovers and offensive
rebounding (the six factors, three per end) explain team scoring as well as
the classic four factors do, and does free-throw rate still add anything
once true shooting is in?

From the lineup stints (data/lineups/{season}.parquet, 2019-20 on), every
team-game becomes one row: points per 100 possessions and its factors. Games
whose stint tallies miss the box score are left out, as in the RAPM split.
Three models, each weighted by possessions:

  four    eFG%, TOV%, OREB%, FT rate (FTM/FGA)       what is built now
  six     TS%,  TOV%, OREB%                          the proposal
  six+ft  TS%,  TOV%, OREB%, FT rate                 does FT rate add anything?

For each: R² on all games, and R² for every season predicted by a model
fitted on the other seasons (so it is not graded on games it was fitted to).
Plus what each factor is worth in points per 100, and how correlated the
factors are with each other.

TS% = PTS / (2 × (FGA + 0.44 × FTA)), the league's usual definition.

    python3 check_six_factors.py
"""
import os
import sys

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
LINEUPS = os.path.join(HOME, "data", "lineups")
SEASONS = ["%d-%s" % (y, str(y + 1)[2:]) for y in range(2019, 2027)]
NEED = ["fgm", "fga", "fg3m", "fta", "ftm", "orb", "drb", "tov"]


def team_games():
    import duckdb
    con = duckdb.connect()
    rows = []
    for s in SEASONS:
        path = os.path.join(LINEUPS, "%s.parquet" % s)
        if not os.path.exists(path):
            continue
        have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()}
        if not all("home_%s" % k in have for k in NEED):
            print("  %s: lineup file has no tallies — skipped" % s)
            continue
        bad = set()
        g = os.path.join(LINEUPS, "%s_games.parquet" % s)
        if os.path.exists(g):
            gh = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [g]).fetchall()}
            if "factors_match" in gh:
                bad = {r[0] for r in con.execute(
                    "SELECT game_id FROM read_parquet(?) WHERE factors_match = false", [g]).fetchall()}
        cols = ", ".join("SUM(%s_%s)" % (side, k) for side in ("home", "away") for k in NEED)
        for r in con.execute("""
            SELECT game_id, SUM(home_pts), SUM(away_pts), SUM(home_poss), SUM(away_poss), %s
            FROM read_parquet(?) WHERE reliable AND substr(game_id, 1, 3) = '002'
            GROUP BY 1""" % cols, [path]).fetchall():
            if r[0] in bad:
                continue
            n = len(NEED)
            h = dict(zip(NEED, r[5:5 + n])); a = dict(zip(NEED, r[5 + n:5 + 2 * n]))
            for pts, poss, o, d in ((r[1], r[3], h, a), (r[2], r[4], a, h)):
                rows.append((s, pts or 0, poss or 0, {k: v or 0 for k, v in o.items()},
                             {k: v or 0 for k, v in d.items()}))
    return rows


def features(o, d, poss, pts):
    tsa = o["fga"] + 0.44 * o["fta"]
    reb = o["orb"] + d["drb"]
    if poss < 40 or not o["fga"] or not reb or not tsa:
        return None
    return {
        "efg": (o["fgm"] + 0.5 * o["fg3m"]) / o["fga"],
        "ts": pts / (2 * tsa),
        "tov": o["tov"] / poss,
        "orb": o["orb"] / reb,
        "ft": o["ftm"] / o["fga"],
    }


MODELS = {
    "four":   ["efg", "tov", "orb", "ft"],
    "six":    ["ts", "tov", "orb"],
    "six+ft": ["ts", "tov", "orb", "ft"],
}


def fit(X, y, w):
    sw = np.sqrt(w)
    A = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    return beta


def r2(X, y, w, beta):
    A = np.column_stack([np.ones(len(X)), X])
    res = y - A @ beta
    mu = np.average(y, weights=w)
    return 1 - np.sum(w * res ** 2) / np.sum(w * (y - mu) ** 2)


def main():
    rows = team_games()
    data = []
    for s, pts, poss, o, d in rows:
        f = features(o, d, poss, pts)
        if f:
            data.append((s, 100.0 * pts / poss, poss, f))
    if len(data) < 500:
        sys.exit("  only %d team-games — build lineups with tallies first" % len(data))
    seasons = sorted({r[0] for r in data})
    y = np.array([r[1] for r in data]); w = np.array([r[2] for r in data], dtype=float)
    S = np.array([r[0] for r in data])
    print("\n  %d team-games, %s to %s" % (len(data), seasons[0], seasons[-1]))

    print("\n  model    R² all   R² held-out season   worth per 100 possessions (per 1.00 of each rate)")
    for name, keys in MODELS.items():
        X = np.array([[r[3][k] for k in keys] for r in data])
        beta = fit(X, y, w)
        held = []
        for s in seasons:
            tr, te = S != s, S == s
            b = fit(X[tr], y[tr], w[tr])
            held.append(r2(X[te], y[te], w[te], b))
        print("  %-7s  %.3f    %.3f (lowest %.3f)     %s" % (
            name, r2(X, y, w, beta), np.mean(held), min(held),
            "  ".join("%s %+.0f" % (k, v) for k, v in zip(keys, beta[1:]))))

    # how much each factor moves points in practice: worth × one league standard deviation
    print("\n  one standard deviation of each factor, in points per 100 (six+ft model):")
    keys = MODELS["six+ft"]
    X = np.array([[r[3][k] for k in keys] for r in data])
    beta = fit(X, y, w)
    for k, b, col in zip(keys, beta[1:], X.T):
        sd = np.sqrt(np.cov(col, aweights=w))
        print("    %-4s sd %.3f  →  %+.1f points per 100" % (k, sd, b * sd))

    print("\n  correlation between factors (team-games):")
    allk = ["efg", "ts", "tov", "orb", "ft"]
    M = np.array([[r[3][k] for k in allk] for r in data])
    C = np.corrcoef(M.T)
    print("          " + "  ".join("%6s" % k for k in allk))
    for i, k in enumerate(allk):
        print("    %-4s  " % k + "  ".join("%6.2f" % C[i, j] for j in range(len(allk))))


if __name__ == "__main__":
    main()
