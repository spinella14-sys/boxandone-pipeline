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

--luck fits the luck-adjusted version. In every stint, the made threes and
free throws are replaced by what those attempts are expected to yield: the
offensive five's own three-point and free-throw percentages for the season
(attempt-weighted, regressed toward the league), times the attempts. Shooting
skill stays — a good shooting lineup still expects more — but the run of
makes and misses doesn't, and a defence is credited for the threes and free
throws it allows, not for how many happened to go in. Written as
o_lprapm / d_lprapm / lprapm, to v2/models/lprapm/.

This is one of several models. JARED is the main one Box and One is building;
this, RAPM and BPM are inputs to it and checks against it.

  o_prapm, d_prapm, prapm   points per 100 above average (defence: prevented)
  o_prior, d_prior          the box-score prior the fit started from
  k, lam                    chosen prior weight and penalty

    python3 compute_prior_rapm.py check --season 2025-26     # fit and print, write nothing
    python3 compute_prior_rapm.py build --seasons 2019-20:2026-27
    python3 compute_prior_rapm.py build                       # current season (nightly)
    python3 compute_prior_rapm.py check --season 2025-26 --luck   # the luck-adjusted version

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

K_GRID = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5]
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


# regression of shooting toward the league: this many attempts at league rate
REG_3PA, REG_FTA = 150.0, 60.0


def shooting(season_list, s3, bucket, tmp, ours_to_nba):
    """{nba_id: (3P%, FT%, 3PA, FTA)} over the window, regressed toward the
    league, plus the league's own (3P%, FT%)."""
    import duckdb
    con = duckdb.connect()
    acc, lg = {}, [0.0, 0.0, 0.0, 0.0]
    for s in season_list:
        p = os.path.join(tmp, "ps_%s.parquet" % s)
        if not os.path.exists(p):
            try:
                s3.download_file(bucket, "v2/seasons/%s/player_season.parquet" % s, p)
            except Exception:
                continue
        for pid, m3, a3, mf, af in con.execute("""
                SELECT player_id, SUM(fg3m), SUM(fg3a), SUM(ftm), SUM(fta) FROM read_parquet(?)
                WHERE season_type = 'regular' GROUP BY 1""", [p]).fetchall():
            nba = ours_to_nba.get(pid)
            vals = [m3 or 0, a3 or 0, mf or 0, af or 0]
            for i in range(4):
                lg[i] += vals[i]
            if nba is None:
                continue
            a = acc.setdefault(nba, [0.0, 0.0, 0.0, 0.0])
            for i in range(4):
                a[i] += vals[i]
    l3 = lg[0] / lg[1] if lg[1] else 0.36
    lf = lg[2] / lg[3] if lg[3] else 0.78
    out = {nba: ((m3 + REG_3PA * l3) / (a3 + REG_3PA), (mf + REG_FTA * lf) / (af + REG_FTA), a3, af)
           for nba, (m3, a3, mf, af) in acc.items()}
    return out, (l3, lf)


def load_obs_luck(paths, shoot, league):
    """As compute_rapm.load_obs, with each observation's points made
    luck-neutral: actual threes and free throws out, expected ones in."""
    import duckdb
    con = duckdb.connect()
    l3, lf = league
    obs, skipped = [], 0

    def expect(ids):
        # the five's percentages, weighted by how many of each they take
        w3 = sum(shoot.get(p, (0, 0, 0, 0))[2] for p in ids)
        wf = sum(shoot.get(p, (0, 0, 0, 0))[3] for p in ids)
        e3 = sum(shoot[p][0] * shoot[p][2] for p in ids if p in shoot) / w3 if w3 else l3
        ef = sum(shoot[p][1] * shoot[p][3] for p in ids if p in shoot) / wf if wf else lf
        return e3, ef

    for p in paths:
        have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [p]).fetchall()}
        if not all("%s_%s" % (s, k) in have for s in ("home", "away") for k in ("fg3m", "fg3a", "ftm", "fta")):
            print("  %s: stints carry no shooting tallies — rebuild lineups first" % os.path.basename(p))
            continue
        rows = con.execute("""
            SELECT game_id, home_ids, away_ids, home_pts, away_pts, home_poss, away_poss, seconds,
                   home_fg3m, home_fg3a, home_ftm, home_fta, away_fg3m, away_fg3a, away_ftm, away_fta
            FROM read_parquet(?) WHERE reliable AND substr(game_id, 1, 3) = '002'""", [p]).fetchall()
        for r in rows:
            gid, hids, aids, hp, ap, hpo, apo, secs = r[:8]
            h = [int(x) for x in (hids or "").split(",") if x]
            a = [int(x) for x in (aids or "").split(",") if x]
            if len(h) != 5 or len(a) != 5:
                skipped += 1
                continue
            for off, de, home, pts, poss, (m3, a3, mf, af) in (
                    (h, a, 1, hp, hpo, r[8:12]), (a, h, 0, ap, apo, r[12:16])):
                if not poss:
                    continue
                e3, ef = expect(off)
                adj = (pts or 0) - 3 * (m3 or 0) - (mf or 0) + 3 * (a3 or 0) * e3 + (af or 0) * ef
                obs.append((off, de, home, adj, poss, secs or 0, gid))
    return obs


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


def run_season(season, s3, bucket, tmp, ids, names, upload, luck=False):
    ours_to_nba = {v: k for k, v in ids.items()}
    rows, meta = [], {}
    for window, span in (("1y", [season]), ("3y", [R.prev(season, 2), R.prev(season), season])):
        span = [s for s in span if s >= R.FIRST]
        paths = [p for p in (R.stint_file(s, s3, bucket, tmp, upload) for s in span) if p]
        if not paths:
            continue
        if luck:
            shoot, league = shooting(span, s3, bucket, tmp, ours_to_nba)
            obs = load_obs_luck(paths, shoot, league)
        else:
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
            if luck:
                r = {(k.replace("prapm", "lprapm") if "prapm" in k else k): v for k, v in r.items()}
            rows.append(dict(r, season=season, window=window, player_id=ids.get(r["nba_id"]),
                             full_name=names.get(ids.get(r["nba_id"])), lam=m["lambda"], k=m["k"],
                             seasons=",".join(span)))
    return rows, meta


def show(rows, window, n=20, min_min=1000):
    luck = rows and "lprapm" in rows[0]
    k = "lprapm" if luck else "prapm"
    rows = [dict(r, prapm=r[k], o_prapm=r["o_" + k], d_prapm=r["d_" + k]) for r in rows]
    pool = sorted([r for r in rows if r["window"] == window and r["minutes"] >= min_min], key=lambda r: -r["prapm"])
    print("\n  %s, %d+ minutes on the floor — %s (prior = shrunk BPM)"
          % (window, min_min, "LUCK-ADJUSTED box-prior RAPM" if luck else "box-prior RAPM"))
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


def combine(s3, bucket, tmp, model="prapm"):
    import duckdb
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="v2/models/%s/" % model):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    paths = []
    for i, key in enumerate(keys):
        p = os.path.join(tmp, "p%d.parquet" % i)
        s3.download_file(bucket, key, p)
        paths.append(p)
    if paths:
        out = os.path.join(tmp, "%s_all.parquet" % model)
        duckdb.connect().execute("COPY (SELECT * FROM read_parquet(%s, union_by_name=true)) TO '%s' (FORMAT PARQUET)"
                                 % (repr(paths), out))
        s3.upload_file(out, bucket, "v2/models/%s.parquet" % model)
        print("  %s.parquet: %d seasons" % (model, len(paths)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    ap.add_argument("--luck", action="store_true", help="the luck-adjusted version")
    a = ap.parse_args()
    model = "lprapm" if a.luck else "prapm"
    spec = a.seasons or a.season or R.current_season()
    seasons = [s for s in R.seasons_from(spec) if s >= R.FIRST]
    s3, bucket = R.r2()
    ids = R.nba_to_ours()
    names = R.names_map()
    with tempfile.TemporaryDirectory() as tmp:
        for season in seasons:
            rows, _ = run_season(season, s3, bucket, tmp, ids, names, upload=False, luck=a.luck)
            if not rows:
                continue
            if a.cmd == "check":
                show(rows, "1y")
                show(rows, "3y")
                continue
            p = os.path.join(tmp, "%s_%s.parquet" % (model, season))
            R.write(rows, p)
            s3.upload_file(p, bucket, "v2/models/%s/%s.parquet" % (model, season))
        if a.cmd == "build":
            combine(s3, bucket, tmp, model)


if __name__ == "__main__":
    main()
