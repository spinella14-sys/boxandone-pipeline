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


def load_obs(paths):
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


def design(obs, index):
    """Sparse rows as index arrays: 5 offence columns, 5 defence columns, home."""
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


def fit(obs, folds=5, seed=7):
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
    poss_o = np.zeros(n_p)
    poss_d = np.zeros(n_p)
    secs = np.zeros(n_p)
    for (off, de, _, _, poss, s, _) in obs:
        for p in off:
            poss_o[index[p]] += poss
            secs[index[p]] += s
        for p in de:
            poss_d[index[p]] += poss
    out = []
    for p, i in index.items():
        o, d = coef[i], -coef[n_p + i]
        out.append({"nba_id": p, "o_rapm": round(float(o), 3), "d_rapm": round(float(d), 3),
                    "rapm": round(float(o + d), 3), "off_poss": int(poss_o[i]),
                    "def_poss": int(poss_d[i]), "minutes": round(float(secs[i]) / 60.0, 1)})
    return out, {"lambda": lam, "home": round(float(coef[-1]), 3), "mean": round(mu, 3),
                 "obs": len(obs), "players": n_p}


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
        obs = load_obs(paths)
        if len(obs) < 2000:
            print("  %s %s: only %d observations — skipped" % (season, window, len(obs)))
            continue
        res, m = fit(obs)
        meta[window] = m
        print("  %s %-2s  %6d obs · %4d players · lambda %5d · home %+.2f · mean %.1f"
              % (season, window, m["obs"], m["players"], m["lambda"], m["home"], m["mean"]))
        for r in res:
            rows.append(dict(r, season=season, window=window, player_id=ids.get(r["nba_id"]),
                             full_name=names.get(ids.get(r["nba_id"])), lam=m["lambda"],
                             seasons=",".join(span)))
    return rows, meta


def show(rows, window="1y", n=15, min_min=1000):
    pool = [r for r in rows if r["window"] == window and r["minutes"] >= min_min]
    pool.sort(key=lambda r: -r["rapm"])
    print("\n  %s, %d+ minutes: best" % (window, min_min))
    for r in pool[:n]:
        print("    %-24s %+6.2f   O %+5.2f  D %+5.2f   %5.0f min"
              % ((r["full_name"] or r["nba_id"])[:24], r["rapm"], r["o_rapm"], r["d_rapm"], r["minutes"]))
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
