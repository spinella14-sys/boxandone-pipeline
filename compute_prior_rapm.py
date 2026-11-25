#!/usr/bin/env python3
"""
compute_prior_rapm.py — RAPM with a box-score prior ("box-prior RAPM").

Plain RAPM (compute_rapm.py) is a ridge regression that pulls every player
toward zero — an average player — wherever the lineup data can't tell him
apart from his teammates. That makes it noisy for one season and harsh on
low-minute players. The public all-in-one models (EPM, LEBRON, DARKO's
plus-minus) pull each player instead toward what his box score says he is
worth. This is that model, built on our own stints and our own BPM:

  minimise   sum_w (y - Xb)^2  +  lambda * |b - k * prior|^2

  y      points per 100 in each stint observation (offence five vs defence five)
  prior  each player's OBPM on his offence column and -DBPM on his defence
         column (compute_bpm.py), shrunk toward replacement level for low
         minutes, minute-weighted across the seasons of the window
  k      how far to trust the box score — chosen with lambda by five-fold
         cross-validation over whole games, alongside the plain-RAPM grid, so
         the data decide how much the prior earns

Same windows as RAPM: each season alone (1y), and with the two before (3y).
Regular season, reliable games only.

This is one of several models. JARED is the main one Box and One is building;
this, RAPM and BPM are inputs to it and checks against it.

  o_prapm, d_prapm, prapm   points per 100 above average (defence: prevented)
  o_prior, d_prior          the box-score prior the fit started from
  k, lam                    chosen prior weight and penalty

    python3 compute_prior_rapm.py check --season 2025-26     # fit and print, write nothing
    python3 compute_prior_rapm.py build --seasons 2019-20:2026-27
    python3 compute_prior_rapm.py build                       # current season (nightly)

Writes v2/models/prapm/{season}.parquet and v2/models/prapm.parquet.
"""
import argparse
import os
import sys
import tempfile

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import compute_rapm as R  # noqa: E402

K_GRID = [0.0, 0.25, 0.5, 0.75, 1.0]
LAMBDAS = [500, 1000, 2000, 3000, 5000, 8000, 12000, 20000, 30000]
# low-minute box scores are extreme: the prior is shrunk toward replacement
# level (BPM -2, split -1.5 offence / -0.5 defence) by this many minutes
SHRINK_MIN = 300.0
REPL_O, REPL_D = -1.5, -0.5


def priors(season_list, s3, bucket, tmp, ours_to_nba):
    """{nba_id: (obpm, dbpm, minutes)} minute-weighted over the window, shrunk."""
    import duckdb
    acc = {}
    con = duckdb.connect()
    for s in season_list:
        p = os.path.join(tmp, "ps_%s.parquet" % s)
        try:
            s3.download_file(bucket, "v2/seasons/%s/player_season.parquet" % s, p)
        except Exception:
            print("  %s: no player_season file — no prior for that season" % s)
            continue
        cols = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [p]).fetchall()}
        if "obpm" not in cols:
            print("  %s: player_season carries no BPM — run compute_bpm.py first" % s)
            continue
        for pid, mp, ob, db in con.execute("""
                SELECT player_id, SUM(mp), SUM(obpm * mp) / NULLIF(SUM(mp), 0), SUM(dbpm * mp) / NULLIF(SUM(mp), 0)
                FROM read_parquet(?) WHERE season_type = 'regular' AND obpm IS NOT NULL
                GROUP BY 1""", [p]).fetchall():
            nba = ours_to_nba.get(pid)
            if nba is None or not mp:
                continue
            a = acc.setdefault(nba, [0.0, 0.0, 0.0])
            a[0] += ob * mp
            a[1] += db * mp
            a[2] += mp
    out = {}
    for nba, (o, d, mp) in acc.items():
        out[nba] = ((o + SHRINK_MIN * REPL_O) / (mp + SHRINK_MIN),
                    (d + SHRINK_MIN * REPL_D) / (mp + SHRINK_MIN), mp)
    return out


def fit(obs, prior, folds=5, seed=7):
    players = sorted({p for o in obs for p in o[0]} | {p for o in obs for p in o[1]})
    index = {p: i for i, p in enumerate(players)}
    n_p = len(players)
    n_cols = 2 * n_p + 1
    cols, y, w, home = R.design(obs, index)
    # the prior on each column: OBPM on offence, -DBPM on defence (a defence
    # column's coefficient is points allowed), nothing for players without one
    p0 = np.zeros(n_cols)
    for p, i in index.items():
        pr = prior.get(p)
        o, d = (pr[0], pr[1]) if pr else (REPL_O, REPL_D)
        p0[i] = o
        p0[n_p + i] = -d
    games = sorted({o[6] for o in obs})
    rng = np.random.default_rng(seed)
    gfold = dict(zip(games, rng.integers(0, folds, len(games))))
    fold = np.array([gfold[o[6]] for o in obs])
    mu = float(np.average(y, weights=w))

    def solve(A, b, lam, k):
        pen = np.full(n_cols, float(lam))
        pen[-1] = 1.0
        return np.linalg.solve(A + np.diag(pen), b + pen * (k * p0) * (np.arange(n_cols) < n_cols - 1))

    err = {(lam, k): 0.0 for lam in LAMBDAS for k in K_GRID}
    for f in range(folds):
        tr, te = fold != f, fold == f
        A, b = R.normal_eq(cols[tr], y[tr], w[tr], home[tr], n_cols, mu)
        for lam in LAMBDAS:
            for k in K_GRID:
                c = solve(A, b, lam, k)
                pred = mu + R.predict(cols[te], home[te], c)
                err[(lam, k)] += float(np.sum(w[te] * (y[te] - pred) ** 2))
    lam, k = min(err, key=err.get)
    plain = min(e for (l, kk), e in err.items() if kk == 0.0)
    A, b = R.normal_eq(cols, y, w, home, n_cols, mu)
    coef = solve(A, b, lam, k)

    poss_o, secs = np.zeros(n_p), np.zeros(n_p)
    for (off, de, _, _, poss, s, _) in obs:
        for p in off:
            poss_o[index[p]] += poss
            secs[index[p]] += s
    out = []
    for p, i in index.items():
        o, d = coef[i], -coef[n_p + i]
        out.append({"nba_id": p, "o_prapm": round(float(o), 3), "d_prapm": round(float(d), 3),
                    "prapm": round(float(o + d), 3), "o_prior": round(float(p0[i]), 3),
                    "d_prior": round(float(-p0[n_p + i]), 3), "minutes": round(float(secs[i]) / 60.0, 1)})
    gain = 100.0 * (plain - err[(lam, k)]) / plain if plain else 0.0
    meta = {"lambda": lam, "k": k, "mean": round(mu, 3), "home": round(float(coef[-1]), 3),
            "obs": len(obs), "players": n_p, "cv_gain_pct": round(gain, 3)}
    return out, meta


def run_season(season, s3, bucket, tmp, ids, names, upload):
    ours_to_nba = {v: k for k, v in ids.items()}
    rows, meta = [], {}
    for window, span in (("1y", [season]), ("3y", [R.prev(season, 2), R.prev(season), season])):
        span = [s for s in span if s >= R.FIRST]
        paths = [p for p in (R.stint_file(s, s3, bucket, tmp, upload) for s in span) if p]
        if not paths:
            continue
        obs, _ = R.load_obs(paths)
        if len(obs) < 2000:
            print("  %s %s: only %d observations — skipped" % (season, window, len(obs)))
            continue
        pr = priors(span, s3, bucket, tmp, ours_to_nba)
        res, m = fit(obs, pr)
        meta[window] = m
        print("  %s %-2s  %6d obs · %4d players · lambda %5d · prior weight k %.2f · "
              "out-of-sample error %.2f%% below plain RAPM"
              % (season, window, m["obs"], m["players"], m["lambda"], m["k"], m["cv_gain_pct"]))
        for r in res:
            rows.append(dict(r, season=season, window=window, player_id=ids.get(r["nba_id"]),
                             full_name=names.get(ids.get(r["nba_id"])), lam=m["lambda"], k=m["k"],
                             seasons=",".join(span)))
    return rows, meta


def show(rows, window, n=20, min_min=1000):
    pool = sorted([r for r in rows if r["window"] == window and r["minutes"] >= min_min], key=lambda r: -r["prapm"])
    print("\n  %s, %d+ minutes on the floor — box-prior RAPM (prior = shrunk BPM)" % (window, min_min))
    print("    %-24s %7s  %6s %6s   %7s   %6s" % ("", "PRAPM", "O", "D", "prior", "min"))
    for r in pool[:n]:
        print("    %-24s %+7.2f  %+6.2f %+6.2f   %+7.2f   %6.0f" % (
            (r["full_name"] or str(r["nba_id"]))[:24], r["prapm"], r["o_prapm"], r["d_prapm"],
            r["o_prior"] + r["d_prior"], r["minutes"]))
    print("  … bottom five")
    for r in pool[-5:]:
        print("    %-24s %+7.2f  %+6.2f %+6.2f   %+7.2f   %6.0f" % (
            (r["full_name"] or str(r["nba_id"]))[:24], r["prapm"], r["o_prapm"], r["d_prapm"],
            r["o_prior"] + r["d_prior"], r["minutes"]))


def combine(s3, bucket, tmp):
    import duckdb
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/models/prapm/"):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    paths = []
    for i, key in enumerate(keys):
        p = os.path.join(tmp, "p%d.parquet" % i)
        s3.download_file(bucket, key, p)
        paths.append(p)
    if paths:
        out = os.path.join(tmp, "prapm_all.parquet")
        duckdb.connect().execute("COPY (SELECT * FROM read_parquet(%s, union_by_name=true)) TO '%s' (FORMAT PARQUET)"
                                 % (repr(paths), out))
        s3.upload_file(out, bucket, "v2/models/prapm.parquet")
        print("  prapm.parquet: %d seasons" % len(paths))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    a = ap.parse_args()
    spec = a.seasons or a.season or R.current_season()
    seasons = [s for s in R.seasons_from(spec) if s >= R.FIRST]
    s3, bucket = R.r2()
    ids = R.nba_to_ours()
    names = R.names_map()
    with tempfile.TemporaryDirectory() as tmp:
        for season in seasons:
            rows, _ = run_season(season, s3, bucket, tmp, ids, names, upload=False)
            if not rows:
                continue
            if a.cmd == "check":
                show(rows, "1y")
                show(rows, "3y")
                continue
            p = os.path.join(tmp, "prapm_%s.parquet" % season)
            R.write(rows, p)
            s3.upload_file(p, bucket, "v2/models/prapm/%s.parquet" % season)
        if a.cmd == "build":
            combine(s3, bucket, tmp)


if __name__ == "__main__":
    main()
