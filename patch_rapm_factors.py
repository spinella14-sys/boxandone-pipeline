#!/usr/bin/env python3
"""
patch_rapm_factors.py — run once, after patch_lineups_factors.py and a lineup rebuild.

compute_rapm.py splits RAPM into the four factors on both ends, using the
box-score tallies the lineup builder now writes into every stint:

  shooting       eFG%          (FGM + 0.5·3PM) / FGA          weighted by FGA
  ball security  TOV%          TOV / possessions               weighted by possessions
  crashing       OREB%         OREB / (OREB + opponent DREB)   weighted by those chances
  free throws    FT rate       FTM / FGA                       weighted by FGA

Each is its own ridge regression, the same design as RAPM (five offence
columns, five defence columns, home court), with its own penalty chosen by the
same five-fold game-level cross-validation — the factors are not equally noisy.

Each player gets the factor in its own units (percentage points: o_efg +1.8 is
"his units shoot 1.8 points of eFG% better than the players around him
explain"), and in points per 100 possessions (o_efg_pts), using how much each
factor is worth in points that season, fitted from the team-game rows of the
same stints. Every column is signed so that positive is good: d_tov +1.5 means
his defences force turnovers on 1.5% more possessions.

The eight point columns sum close to RAPM but not exactly — each is shrunk
separately, and the four factors do not explain every point (the last few
percent are things like and-ones and the free throws that end possessions).
`check` prints how closely they agree.

Seasons whose lineup files predate the tallies keep RAPM and leave the factor
columns blank.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "compute_rapm.py")
src = open(PATH).read()
if "def factor_targets" in src:
    print("  compute_rapm.py already patched")
    sys.exit()

PAIRS = []

# 1. load the tallies alongside each observation
PAIRS.append(('''def load_obs(paths):
    """Two observations per stint: (offence ids, defence ids, offence is home,
    points, possessions, seconds, game)."""
    import duckdb
    con = duckdb.connect()
    obs = []
    for p in paths:
        rows = con.execute("""
            SELECT game_id, home_ids, away_ids, home_pts, away_pts, home_poss, away_poss, seconds
            FROM read_parquet(?)
            WHERE reliable AND substr(game_id, 1, 3) = '002'""", [p]).fetchall()
        for gid, hids, aids, hp, ap, hpo, apo, secs in rows:
            h = [int(x) for x in (hids or "").split(",") if x]
            a = [int(x) for x in (aids or "").split(",") if x]
            if len(h) != 5 or len(a) != 5:
                continue
            if hpo:
                obs.append((h, a, 1, hp or 0, hpo, secs or 0, gid))
            if apo:
                obs.append((a, h, 0, ap or 0, apo, secs or 0, gid))
    return obs
''', '''FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
NEED = ("fgm", "fga", "fg3m", "ftm", "orb", "drb", "tov")


def load_obs(paths):
    """Two observations per stint: (offence ids, defence ids, offence is home,
    points, possessions, seconds, game). Returns (obs, tallies): tallies runs
    parallel to obs as (offence's, defence's) dicts, or is None when any file
    predates the per-stint box score."""
    import duckdb
    con = duckdb.connect()
    obs, tal = [], []
    for p in paths:
        have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [p]).fetchall()}
        fx = all("home_%s" % k in have for k in NEED)
        if not fx:
            tal = None
        fsql = ", " + ", ".join("%s_%s" % (s, k) for s in ("home", "away") for k in NEED) if fx else ""
        rows = con.execute("""
            SELECT game_id, home_ids, away_ids, home_pts, away_pts, home_poss, away_poss, seconds%s
            FROM read_parquet(?)
            WHERE reliable AND substr(game_id, 1, 3) = '002'""" % fsql, [p]).fetchall()
        n = len(NEED)
        for r in rows:
            gid, hids, aids, hp, ap, hpo, apo, secs = r[:8]
            h = [int(x) for x in (hids or "").split(",") if x]
            a = [int(x) for x in (aids or "").split(",") if x]
            if len(h) != 5 or len(a) != 5:
                continue
            ht = dict(zip(NEED, (v or 0 for v in r[8:8 + n]))) if fx else None
            at = dict(zip(NEED, (v or 0 for v in r[8 + n:8 + 2 * n]))) if fx else None
            if hpo:
                obs.append((h, a, 1, hp or 0, hpo, secs or 0, gid))
                if tal is not None:
                    tal.append((ht, at))
            if apo:
                obs.append((a, h, 0, ap or 0, apo, secs or 0, gid))
                if tal is not None:
                    tal.append((at, ht))
    return obs, tal


# name, rate (offence's tallies, defence's tallies, possessions) -> (value, weight),
# and the sign that makes "more of this" good for the offence
FACTOR_DEFS = (
    ("efg", lambda o, d, q: ((o["fgm"] + 0.5 * o["fg3m"]) / o["fga"], o["fga"]) if o["fga"] else (0.0, 0.0), 1),
    ("tov", lambda o, d, q: (o["tov"] / q, q) if q else (0.0, 0.0), -1),
    ("orb", lambda o, d, q: (o["orb"] / (o["orb"] + d["drb"]), o["orb"] + d["drb"])
                            if o["orb"] + d["drb"] else (0.0, 0.0), 1),
    ("ft", lambda o, d, q: (o["ftm"] / o["fga"], o["fga"]) if o["fga"] else (0.0, 0.0), 1),
)


def factor_targets(obs, tal):
    """{factor: (y, w)} per observation."""
    out = {}
    for name, f, _ in FACTOR_DEFS:
        vals = [f(o, d, ob[4]) for ob, (o, d) in zip(obs, tal)]
        out[name] = (np.array([v[0] for v in vals]), np.array([float(v[1]) for v in vals]))
    return out


def factor_worth(obs, tal):
    """Points per 100 possessions per unit of each factor, fitted on team-games.
    Returns {factor: slope} in the factor's own units (a rate, not a percent)."""
    agg = {}
    for ob, (o, d) in zip(obs, tal):
        k = (ob[6], ob[2])
        a = agg.setdefault(k, {"pts": 0, "poss": 0, "fgm": 0, "fga": 0, "fg3m": 0, "ftm": 0,
                               "orb": 0, "opp_drb": 0, "tov": 0})
        a["pts"] += ob[3]
        a["poss"] += ob[4]
        for x in ("fgm", "fga", "fg3m", "ftm", "orb", "tov"):
            a[x] += o[x]
        a["opp_drb"] += d["drb"]
    X, y, w = [], [], []
    for a in agg.values():
        if a["poss"] < 40 or not a["fga"] or not (a["orb"] + a["opp_drb"]):
            continue
        X.append([1.0, (a["fgm"] + 0.5 * a["fg3m"]) / a["fga"], a["tov"] / a["poss"],
                  a["orb"] / (a["orb"] + a["opp_drb"]), a["ftm"] / a["fga"]])
        y.append(100.0 * a["pts"] / a["poss"])
        w.append(a["poss"])
    if len(X) < 50:
        return None
    X, y, w = np.array(X), np.array(y), np.sqrt(np.array(w, dtype=float))
    beta, *_ = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)
    pred = X @ beta
    r2 = 1 - np.sum((w * (y - pred)) ** 2) / np.sum((w * (y - np.average(y, weights=w ** 2))) ** 2)
    return {"efg": float(beta[1]), "tov": float(beta[2]), "orb": float(beta[3]),
            "ft": float(beta[4]), "r2": float(r2)}
''', 1))

# 2. design() takes an optional target
PAIRS.append(('''def design(obs, index):
    """Sparse rows as index arrays: 5 offence columns, 5 defence columns, home."""''',
              '''def design(obs, index):
    """Sparse rows as index arrays: 5 offence columns, 5 defence columns, home.
    y and w are points per 100 and possessions; factor fits swap in their own."""''', 1))

# 3. the cross-validated ridge becomes reusable
PAIRS.append(('''def fit(obs, folds=5, seed=7):
    players = sorted({p for o in obs for p in o[0]} | {p for o in obs for p in o[1]})
    index = {p: i for i, p in enumerate(players)}
    n_p = len(players)
    n_cols = 2 * n_p + 1
    cols, y, w, home = design(obs, index)
    mu = float(np.average(y, weights=w))

    # five folds of whole games, so a game is never split between fit and test
    games = sorted({o[6] for o in obs})
    rng = np.random.default_rng(seed)
    gfold = dict(zip(games, rng.integers(0, folds, len(games))))
    fold = np.array([gfold[o[6]] for o in obs])
    err = {lam: 0.0 for lam in LAMBDAS}
    for f in range(folds):
        tr, te = fold != f, fold == f
        A, bvec = normal_eq(cols[tr], y[tr], w[tr], home[tr], n_cols, mu)
        for lam in LAMBDAS:
            c = solve(A, bvec, lam, n_p)
            pred = mu + predict(cols[te], home[te], c)
            err[lam] += float(np.sum(w[te] * (y[te] - pred) ** 2))
    lam = min(err, key=err.get)

    A, bvec = normal_eq(cols, y, w, home, n_cols, mu)
    coef = solve(A, bvec, lam, n_p)
''', '''def ridge_cv(cols, y, w, home, n_cols, n_p, fold, folds=5, lambdas=None):
    """Ridge with the penalty picked by game-level cross-validation -> (coef, lambda, mean)."""
    lambdas = lambdas or LAMBDAS
    mu = float(np.average(y, weights=w))
    err = {lam: 0.0 for lam in lambdas}
    for f in range(folds):
        tr, te = fold != f, fold == f
        A, bvec = normal_eq(cols[tr], y[tr], w[tr], home[tr], n_cols, mu)
        for lam in lambdas:
            c = solve(A, bvec, lam, n_p)
            pred = mu + predict(cols[te], home[te], c)
            err[lam] += float(np.sum(w[te] * (y[te] - pred) ** 2))
    lam = min(err, key=err.get)
    A, bvec = normal_eq(cols, y, w, home, n_cols, mu)
    return solve(A, bvec, lam, n_p), lam, mu


# factor rates are fractions weighted by attempts, so their penalties sit on a
# different scale from points per 100; the grid is wide and CV picks
FACTOR_LAMBDAS = [50, 100, 250, 500, 1000, 2000, 4000, 8000, 16000]


def fit(obs, tal=None, folds=5, seed=7):
    players = sorted({p for o in obs for p in o[0]} | {p for o in obs for p in o[1]})
    index = {p: i for i, p in enumerate(players)}
    n_p = len(players)
    n_cols = 2 * n_p + 1
    cols, y, w, home = design(obs, index)

    # five folds of whole games, so a game is never split between fit and test
    games = sorted({o[6] for o in obs})
    rng = np.random.default_rng(seed)
    gfold = dict(zip(games, rng.integers(0, folds, len(games))))
    fold = np.array([gfold[o[6]] for o in obs])
    coef, lam, mu = ridge_cv(cols, y, w, home, n_cols, n_p, fold, folds)

    factors, worth = {}, None
    if tal is not None:
        worth = factor_worth(obs, tal)
        if worth:
            for name, (fy, fw) in factor_targets(obs, tal).items():
                c, fl, fm = ridge_cv(cols, fy, fw, home, n_cols, n_p, fold, folds, FACTOR_LAMBDAS)
                factors[name] = (c, fl, fm)
''', 1))

# 4. per-player factor columns
PAIRS.append(('''    out = []
    for p, i in index.items():
        o, d = coef[i], -coef[n_p + i]
        out.append({"nba_id": p, "o_rapm": round(float(o), 3), "d_rapm": round(float(d), 3),
                    "rapm": round(float(o + d), 3), "off_poss": int(poss_o[i]),
                    "def_poss": int(poss_d[i]), "minutes": round(float(secs[i]) / 60.0, 1)})
    return out, {"lambda": lam, "home": round(float(coef[-1]), 3), "mean": round(mu, 3),
                 "obs": len(obs), "players": n_p}
''', '''    sign = {name: s for name, _, s in FACTOR_DEFS}
    out = []
    for p, i in index.items():
        o, d = coef[i], -coef[n_p + i]
        row = {"nba_id": p, "o_rapm": round(float(o), 3), "d_rapm": round(float(d), 3),
               "rapm": round(float(o + d), 3), "off_poss": int(poss_o[i]),
               "def_poss": int(poss_d[i]), "minutes": round(float(secs[i]) / 60.0, 1)}
        for name, (c, _, _) in factors.items():
            fo, fd = c[i], c[n_p + i]          # raw: his offences' rate, his defences' opponents' rate
            row["o_%s" % name] = round(100.0 * sign[name] * float(fo), 3)
            row["d_%s" % name] = round(-100.0 * sign[name] * float(fd), 3)
            row["o_%s_pts" % name] = round(worth[name] * float(fo), 3)
            row["d_%s_pts" % name] = round(-worth[name] * float(fd), 3)
        out.append(row)
    meta = {"lambda": lam, "home": round(float(coef[-1]), 3), "mean": round(mu, 3),
            "obs": len(obs), "players": n_p}
    if factors:
        meta["factors"] = {k: {"lambda": v[1], "mean": round(v[2], 4)} for k, v in factors.items()}
        meta["worth"] = {k: round(v, 2) for k, v in worth.items()}
    return out, meta
''', 1))

# 5. run_season passes the tallies through and reports them
PAIRS.append(('''        obs = load_obs(paths)
        if len(obs) < 2000:
            print("  %s %s: only %d observations — skipped" % (season, window, len(obs)))
            continue
        res, m = fit(obs)
        meta[window] = m
        print("  %s %-2s  %6d obs · %4d players · lambda %5d · home %+.2f · mean %.1f"
              % (season, window, m["obs"], m["players"], m["lambda"], m["home"], m["mean"]))
''', '''        obs, tal = load_obs(paths)
        if len(obs) < 2000:
            print("  %s %s: only %d observations — skipped" % (season, window, len(obs)))
            continue
        res, m = fit(obs, tal)
        meta[window] = m
        print("  %s %-2s  %6d obs · %4d players · lambda %5d · home %+.2f · mean %.1f"
              % (season, window, m["obs"], m["players"], m["lambda"], m["home"], m["mean"]))
        if m.get("factors"):
            wv = m["worth"]
            print("            factors: lambda %s · worth per 100 poss: eFG %+.0f, TOV %+.0f, "
                  "OREB %+.0f, FT %+.0f (R² %.2f)"
                  % ("/".join(str(m["factors"][k]["lambda"]) for k in ("efg", "tov", "orb", "ft")),
                     wv["efg"], wv["tov"], wv["orb"], wv["ft"], wv["r2"]))
        else:
            print("            factors: not in these lineup files — rebuild lineups for the split")
''', 1))

# 6. check shows the split and how it adds up
PAIRS.append(('''def show(rows, window="1y", n=15, min_min=1000):
    pool = [r for r in rows if r["window"] == window and r["minutes"] >= min_min]
    pool.sort(key=lambda r: -r["rapm"])
    print("\\n  %s, %d+ minutes: best" % (window, min_min))
    for r in pool[:n]:
        print("    %-24s %+6.2f   O %+5.2f  D %+5.2f   %5.0f min"
              % ((r["full_name"] or r["nba_id"])[:24], r["rapm"], r["o_rapm"], r["d_rapm"], r["minutes"]))
''', '''def show(rows, window="1y", n=15, min_min=1000):
    pool = [r for r in rows if r["window"] == window and r["minutes"] >= min_min]
    pool.sort(key=lambda r: -r["rapm"])
    print("\\n  %s, %d+ minutes: best" % (window, min_min))
    fx = pool and "o_efg_pts" in pool[0]
    if fx:
        print("    %-24s %6s   %5s  %5s   %s" % ("", "RAPM", "O", "D",
              "offence: eFG  TOV  OREB  FT   |  defence: eFG  TOV  OREB  FT   (points per 100)"))
    for r in pool[:n]:
        line = ("    %-24s %+6.2f   O %+5.2f  D %+5.2f   %5.0f min"
                % ((r["full_name"] or r["nba_id"])[:24], r["rapm"], r["o_rapm"], r["d_rapm"], r["minutes"]))
        if fx:
            line += "   " + " ".join("%+5.1f" % r["o_%s_pts" % k] for k in ("efg", "tov", "orb", "ft"))
            line += "  | " + " ".join("%+5.1f" % r["d_%s_pts" % k] for k in ("efg", "tov", "orb", "ft"))
        print(line)
    if fx:
        so = np.array([sum(r["o_%s_pts" % k] for k in ("efg", "tov", "orb", "ft")) for r in pool])
        sd = np.array([sum(r["d_%s_pts" % k] for k in ("efg", "tov", "orb", "ft")) for r in pool])
        o = np.array([r["o_rapm"] for r in pool])
        d = np.array([r["d_rapm"] for r in pool])
        print("  factor points vs RAPM: offence r=%.2f (slope %.2f), defence r=%.2f (slope %.2f)"
              % (np.corrcoef(so, o)[0, 1], np.polyfit(o, so, 1)[0],
                 np.corrcoef(sd, d)[0, 1], np.polyfit(d, sd, 1)[0]))
''', 1))

for old, new, count in PAIRS:
    n = src.count(old)
    if n != count:
        sys.exit("  compute_rapm.py not as expected (%d× %r) — nothing written" % (n, old[:70]))
    src = src.replace(old, new)
# meta now carries nested dicts; the print in run_season handles them, nothing else reads meta
open(PATH, "w").write(src)
print("  compute_rapm.py patched")
