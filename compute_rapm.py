#!/usr/bin/env python3
"""
compute_rapm.py — Foundation F5, first layer: regularized adjusted plus-minus.

Every stint (a stretch with the same ten players on the floor, from
build_lineups.py) gives two observations: the home five on offence against the
away five on defence, and the reverse. The target is points per 100
possessions in that stint, weighted by its possessions. A ridge regression
then credits each player with how much better or worse his units scored and
defended than the players around and against him explain.

  o_rapm   offence, points per 100 possessions above an average player
  d_rapm   defence, points per 100 prevented (positive is good)
  rapm     o_rapm + d_rapm

The ridge penalty is chosen by five-fold cross-validation over whole games, so
it is fitted to the data, not set by hand; the chosen value is stored with the
results. A home-court term sits in the model so it is not absorbed by players.
Regular-season games only, and only games that passed build_lineups' checks.

Windows: each season alone (1y), and that season with the two before it (3y),
which is what published RAPM usually means and is far more stable.

Stints come from data/lineups/{season}.parquet when built here, and are copied
to v2/lineups/{season}.parquet so the nightly (which has only the current
season's) can fit the 3-year window too.

Writes v2/models/rapm/{season}.parquet and v2/models/rapm.parquet (all seasons).

    python3 compute_rapm.py check --season 2024-25      # fit and print, write nothing
    python3 compute_rapm.py build --seasons 2019-20:2026-27
    python3 compute_rapm.py build                         # the current season (nightly)
"""
import argparse
import os
import sys
import tempfile

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
LINEUPS = os.path.join(HOME, "data", "lineups")
LAMBDAS = [250, 500, 1000, 2000, 3000, 5000, 8000, 12000]
FIRST = "2019-20"     # stints exist from here


def season_label(y):
    return "%d-%s" % (y, str(y + 1)[2:])


def prev(season, n=1):
    return season_label(int(season[:4]) - n)


def current_season():
    from datetime import date
    d = date.today()
    return season_label(d.year if d.month >= 10 else d.year - 1)


def seasons_from(spec):
    lo, hi = spec.split(":") if ":" in spec else (spec, spec)
    return [season_label(y) for y in range(int(lo[:4]), int(hi[:4]) + 1)]


def r2():
    import nightly2 as N
    env = N.load_env()
    return N.s3c(env), env["R2_BUCKET_NAME"]


def stint_file(season, s3, bucket, tmp, upload):
    """The local stints (copied up to R2 on a build); or R2's copy when there is no local one."""
    local = os.path.join(LINEUPS, "%s.parquet" % season)
    key = "v2/lineups/%s.parquet" % season
    if os.path.exists(local):
        if s3 and upload:
            s3.upload_file(local, bucket, key)
        return local
    if not s3:
        return None
    p = os.path.join(tmp, "stints_%s.parquet" % season)
    try:
        s3.download_file(bucket, key, p)
        return p
    except Exception:
        return None


FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
NEED = ("fgm", "fga", "fg3m", "ftm", "orb", "drb", "tov")


def bad_tally_games(season, s3, bucket, tmp):
    """Games whose stint tallies don't add up to the box score (build_lineups)."""
    import duckdb
    local = os.path.join(LINEUPS, "%s_games.parquet" % season)
    path = local if os.path.exists(local) else None
    if not path and s3:
        path = os.path.join(tmp, "games_%s.parquet" % season)
        try:
            s3.download_file(bucket, "v2/lineups/%s_games.parquet" % season, path)
        except Exception:
            return set()
    if not path:
        return set()
    con = duckdb.connect()
    have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()}
    if "factors_match" not in have:
        return set()
    return {r[0] for r in con.execute(
        "SELECT game_id FROM read_parquet(?) WHERE factors_match = false", [path]).fetchall()}


MIN_FACTOR_R2 = 0.80


def load_obs(paths, bad=frozenset()):
    """Two observations per stint: (offence ids, defence ids, offence is home,
    points, possessions, seconds, game). Returns (obs, tallies): tallies runs
    parallel to obs as (offence's, defence's) dicts, or is None when any file
    predates the per-stint box score. A game in `bad` gets None entries: it
    counts for RAPM but not for the factor split."""
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
                    tal.append(None if gid in bad else (ht, at))
            if apo:
                obs.append((a, h, 0, ap or 0, apo, secs or 0, gid))
                if tal is not None:
                    tal.append(None if gid in bad else (at, ht))
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
        vals = [f(t[0], t[1], ob[4]) if t else (0.0, 0.0) for ob, t in zip(obs, tal)]
        out[name] = (np.array([v[0] for v in vals]), np.array([float(v[1]) for v in vals]))
    return out


def factor_worth(obs, tal):
    """Points per 100 possessions per unit of each factor, fitted on team-games.
    Returns {factor: slope} in the factor's own units (a rate, not a percent)."""
    agg = {}
    for ob, t in zip(obs, tal):
        if t is None:
            continue
        o, d = t
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


def design(obs, index):
    """Sparse rows as index arrays: 5 offence columns, 5 defence columns, home.
    y and w are points per 100 and possessions; factor fits swap in their own."""
    n_p = len(index)
    cols = np.empty((len(obs), 11), dtype=np.int64)
    y = np.empty(len(obs))
    w = np.empty(len(obs))
    for i, (off, de, home, pts, poss, _, _) in enumerate(obs):
        cols[i, :5] = [index[p] for p in off]
        cols[i, 5:10] = [n_p + index[p] for p in de]
        cols[i, 10] = 2 * n_p          # home-court column (always present; value 0 or 1)
        y[i] = 100.0 * pts / poss
        w[i] = poss
    home = np.array([o[2] for o in obs], dtype=float)
    return cols, y, w, home


def normal_eq(cols, y, w, home, n_cols, mu):
    """X'WX and X'W(y-mu) without building X."""
    vals = np.ones(cols.shape)
    vals[:, 10] = home
    r = y - mu
    A = np.zeros((n_cols, n_cols))
    b = np.zeros(n_cols)
    for j in range(cols.shape[1]):
        np.add.at(b, cols[:, j], w * vals[:, j] * r)
        for k in range(cols.shape[1]):
            np.add.at(A, (cols[:, j], cols[:, k]), w * vals[:, j] * vals[:, k])
    return A, b


def solve(A, b, lam, n_players):
    pen = np.full(A.shape[0], float(lam))
    pen[-1] = 1.0                      # home court is barely penalised
    return np.linalg.solve(A + np.diag(pen), b)


def predict(cols, home, coef):
    vals = np.ones(cols.shape)
    vals[:, 10] = home
    return (coef[cols] * vals).sum(axis=1)


def ridge_cv(cols, y, w, home, n_cols, n_p, fold, folds=5, lambdas=None):
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

    factors, worth, skipped = {}, None, None
    if tal is not None:
        worth = factor_worth(obs, tal)
        if worth and worth["r2"] < MIN_FACTOR_R2:
            skipped = worth["r2"]
            worth = None
        else:
            skipped = None
        if worth:
            for name, (fy, fw) in factor_targets(obs, tal).items():
                c, fl, fm = ridge_cv(cols, fy, fw, home, n_cols, n_p, fold, folds, FACTOR_LAMBDAS)
                factors[name] = (c, fl, fm)
    poss_o = np.zeros(n_p)
    poss_d = np.zeros(n_p)
    secs = np.zeros(n_p)
    for (off, de, _, _, poss, s, _) in obs:
        for p in off:
            poss_o[index[p]] += poss
            secs[index[p]] += s
        for p in de:
            poss_d[index[p]] += poss
    sign = {name: s for name, _, s in FACTOR_DEFS}
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
    if skipped is not None:
        meta["factors_skipped_r2"] = round(skipped, 2)
    if tal is not None:
        meta["factor_games_left_out"] = len({ob[6] for ob, t in zip(obs, tal) if t is None})
    return out, meta


def nba_to_ours():
    import export_game_lineups as X
    _, ids, _ = X.maps()
    return ids


def run_season(season, s3, bucket, tmp, ids, names, upload=True):
    rows = []
    meta = {}
    for window, span in (("1y", [season]), ("3y", [prev(season, 2), prev(season), season])):
        span = [s for s in span if s >= FIRST]
        paths = [p for p in (stint_file(s, s3, bucket, tmp, upload) for s in span) if p]
        if not paths:
            continue
        bad = set()
        for s in span:
            bad |= bad_tally_games(s, s3, bucket, tmp)
        obs, tal = load_obs(paths, bad)
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
        elif m.get("factors_skipped_r2") is not None:
            print("            factors: NOT published — they explain only R² %.2f of team-game scoring"
                  % m["factors_skipped_r2"])
        else:
            print("            factors: not in these lineup files — rebuild lineups for the split")
        if m.get("factor_games_left_out"):
            print("            (%d games left out of the split: tallies don't match the box score)"
                  % m["factor_games_left_out"])
        for r in res:
            rows.append(dict(r, season=season, window=window, player_id=ids.get(r["nba_id"]),
                             full_name=names.get(ids.get(r["nba_id"])), lam=m["lambda"],
                             seasons=",".join(span)))
    return rows, meta


def show(rows, window="1y", n=15, min_min=1000):
    pool = [r for r in rows if r["window"] == window and r["minutes"] >= min_min]
    pool.sort(key=lambda r: -r["rapm"])
    print("\n  %s, %d+ minutes: best" % (window, min_min))
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
    print("  worst")
    for r in pool[-5:]:
        print("    %-24s %+6.2f   O %+5.2f  D %+5.2f   %5.0f min"
              % ((r["full_name"] or r["nba_id"])[:24], r["rapm"], r["o_rapm"], r["d_rapm"], r["minutes"]))


def names_map():
    import duckdb
    con = duckdb.connect(os.path.join(HOME, "data", "boxandone.duckdb"), read_only=True)
    m = dict(con.execute("SELECT player_id, full_name FROM players").fetchall())
    con.close()
    return m


def write(rows, path):
    import duckdb
    import json
    jl = path + ".jsonl"
    with open(jl, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    duckdb.connect().execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                             "TO '%s' (FORMAT PARQUET)" % (jl, path))


def combine(s3, bucket, tmp):
    import duckdb
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/models/rapm/"):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    paths = []
    for i, k in enumerate(keys):
        p = os.path.join(tmp, "r%d.parquet" % i)
        s3.download_file(bucket, k, p)
        paths.append(p)
    if not paths:
        return
    out = os.path.join(tmp, "rapm_all.parquet")
    duckdb.connect().execute("COPY (SELECT * FROM read_parquet(%s, union_by_name=true)) TO '%s' (FORMAT PARQUET)"
                             % (repr(paths), out))
    s3.upload_file(out, bucket, "v2/models/rapm.parquet")
    print("  rapm.parquet: %d seasons" % len(paths))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    a = ap.parse_args()
    spec = a.seasons or a.season or current_season()
    seasons = [s for s in seasons_from(spec) if s >= FIRST]
    s3, bucket = r2()
    ids = nba_to_ours()
    names = names_map()
    with tempfile.TemporaryDirectory() as tmp:
        for season in seasons:
            rows, meta = run_season(season, s3, bucket, tmp, ids, names, upload=(a.cmd == "build"))
            if not rows:
                continue
            if a.cmd == "check":
                show(rows, "1y")
                show(rows, "3y")
                continue
            p = os.path.join(tmp, "rapm_%s.parquet" % season)
            write(rows, p)
            s3.upload_file(p, bucket, "v2/models/rapm/%s.parquet" % season)
        if a.cmd == "build":
            combine(s3, bucket, tmp)


if __name__ == "__main__":
    main()
