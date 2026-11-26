#!/usr/bin/env python3
"""
compute_tprapm.py — RAPM with a box-score AND tracking prior (EPM-style).

Box-prior RAPM (compute_prior_rapm.py) pulls each player toward his BPM. BPM
reads offence well and defence poorly: the box score only sees steals, blocks
and defensive rebounds. NBA.com's tracking sees more of a defender's work —
deflections, contested shots, loose balls, charges, box-outs, how opponents
shoot at the rim with him there — and of a creator's: touches, time on the
ball, potential assists, shots off the dribble.

So the prior here is learned, not borrowed: a regression of plain three-year
RAPM (offence and defence separately) on the player's per-36 box and tracking
rates over the same three seasons. The target is plain RAPM on purpose — box-
prior RAPM already leans on BPM, so fitting to it would flatter the box score
and hide what tracking adds. Its fitted value
is his prior; the fit then runs exactly as box-prior RAPM does (prior weight
and penalty chosen by five-fold cross-validation over whole games).

The regression's fit is printed beside a box-only regression of the same
targets, so the gain from tracking is visible — expected mostly on defence.

    python3 compute_tprapm.py check --season 2025-26
    python3 compute_tprapm.py build --seasons 2019-20:2026-27
    python3 compute_tprapm.py build                         # current season (nightly)

Writes v2/models/tprapm/{season}.parquet and v2/models/tprapm.parquet
(o_tprapm, d_tprapm, tprapm, and the prior it started from).
"""
import argparse
import os
import sys
import tempfile

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import compute_rapm as R            # noqa: E402
import compute_prior_rapm as P      # noqa: E402

BOX = ["pts", "fga", "fg3a", "fta", "orb", "drb", "ast", "stl", "blk", "tov", "pf"]
TRACK = [  # whichever of these the season's files carry
    "pass_potential_ast", "pass_ast_points_created", "poss_touches", "poss_time_of_poss",
    "drib3_6_fga", "drib7_fga", "hustle_deflections", "hustle_contested_shots",
    "hustle_charges_drawn", "hustle_loose_balls_recovered", "hustle_box_outs",
    "hustle_screen_assists", "dfd_lt6_fga_lt_06", "dfd_lt6_d_fga",
]


def season_table(s, s3, bucket, tmp):
    """{player_id: {stat: total}} for the regular season: box, tracking, hustle."""
    import duckdb
    con = duckdb.connect()
    out = {}
    for name in ("player_season", "tracking", "defense"):
        p = os.path.join(tmp, "%s_%s.parquet" % (name, s))
        if not os.path.exists(p):
            try:
                s3.download_file(bucket, "v2/seasons/%s/%s.parquet" % (s, name), p)
            except Exception:
                continue
        cols = [c[0] for c in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [p]).fetchall()]
        want = [c for c in ["mp"] + BOX + TRACK if c in cols]
        if not want or "player_id" not in cols:
            continue
        rows = con.execute("SELECT player_id, %s FROM read_parquet(?) WHERE season_type = 'regular'"
                           % ", ".join('SUM("%s")' % c if name == "player_season" else 'MAX("%s")' % c for c in want)
                           + " GROUP BY 1", [p]).fetchall()
        for r in rows:
            d = out.setdefault(r[0], {})
            for c, v in zip(want, r[1:]):
                if v is not None:
                    d[c] = float(v)
    return out


def features(d, use):
    mp = d.get("mp") or 0
    if mp <= 0:
        return None
    per = 36.0 / mp
    tsa = d.get("fga", 0) + 0.44 * d.get("fta", 0)
    f = [d.get(k, 0.0) * per for k in BOX]
    f += [d.get("pts", 0) / (2 * tsa) if tsa else 0.5, d.get("fg3a", 0) / d["fga"] if d.get("fga") else 0.0]
    f += [d.get(k, 0.0) * per for k in use]
    return f


def learn(s3, bucket, tmp, seasons):
    """Coefficients for O and D priors: plain three-year RAPM against the
    player's box and tracking totals over the same three seasons (1,500+ minutes)."""
    import duckdb
    p = os.path.join(tmp, "rapm_all.parquet")
    s3.download_file(bucket, "v2/models/rapm.parquet", p)
    tgt = {(pid, s): (float(o), float(d)) for pid, s, o, d in duckdb.connect().execute(
        'SELECT player_id, season, o_rapm, d_rapm FROM read_parquet(?) WHERE "window" = \'3y\' '
        "AND player_id IS NOT NULL", [p]).fetchall()}
    tables = {s: season_table(s, s3, bucket, tmp) for s in seasons}
    # tracking columns present in every season used
    use = [k for k in TRACK if all(any(k in d for d in t.values()) for t in tables.values() if t)]
    X, yo, yd, w = [], [], [], []
    for s in tables:
        span = [x for x in (R.prev(s, 2), R.prev(s), s) if x in tables]
        if len(span) < 3:
            continue
        pids = set().union(*[tables[x].keys() for x in span])
        for pid in pids:
            d = {}
            for x in span:
                for k, v in tables[x].get(pid, {}).items():
                    d[k] = d.get(k, 0.0) + v
            if d.get("mp", 0) < 1500 or (pid, s) not in tgt:
                continue
            f = features(d, use)
            if f is None:
                continue
            X.append(f)
            yo.append(tgt[(pid, s)][0])
            yd.append(tgt[(pid, s)][1])
            w.append(d["mp"])
    X, yo, yd, w = np.array(X), np.array(yo), np.array(yd), np.array(w)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])

    def ridge(Zm, y):
        pen = np.eye(Zm.shape[1]) * 25.0
        pen[0, 0] = 0
        c = np.linalg.solve(Zm.T @ (Zm * w[:, None]) + pen, Zm.T @ (y * w))
        f = Zm @ c
        r2 = 1 - np.sum(w * (y - f) ** 2) / np.sum(w * (y - np.average(y, weights=w)) ** 2)
        return c, r2

    nb = 1 + len(BOX) + 2
    co, r2o = ridge(Z, yo)
    cd, r2d = ridge(Z, yd)
    _, r2o_b = ridge(Z[:, :nb], yo)
    _, r2d_b = ridge(Z[:, :nb], yd)
    print("  prior learned on %d three-season spans (against plain RAPM), %d tracking measures:" % (len(X), len(use)))
    print("    offence R² %.2f (box only %.2f) · defence R² %.2f (box only %.2f)" % (r2o, r2o_b, r2d, r2d_b))
    if len(use) < len(TRACK):
        print("    not used (absent from some season): %s" % ", ".join(k for k in TRACK if k not in use))
    return {"co": co, "cd": cd, "mu": mu, "sd": sd, "use": use, "tables": tables}


def priors(span, model, s3, bucket, tmp, ours_to_nba):
    acc = {}
    for s in span:
        t = model["tables"].get(s) or season_table(s, s3, bucket, tmp)
        for pid, d in t.items():
            f = features(d, model["use"])
            nba = ours_to_nba.get(pid)
            if f is None or nba is None:
                continue
            z = np.concatenate([[1.0], (np.array(f) - model["mu"]) / model["sd"]])
            o, dd, mp = float(z @ model["co"]), float(z @ model["cd"]), d["mp"]
            a = acc.setdefault(nba, [0.0, 0.0, 0.0])
            a[0] += o * mp
            a[1] += dd * mp
            a[2] += mp
    return {nba: ((o + P.SHRINK_MIN * P.REPL_O) / (mp + P.SHRINK_MIN),
                  (d + P.SHRINK_MIN * P.REPL_D) / (mp + P.SHRINK_MIN), mp) for nba, (o, d, mp) in acc.items()}


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
    ours_to_nba = {v: k for k, v in ids.items()}
    with tempfile.TemporaryDirectory() as tmp:
        model = learn(s3, bucket, tmp, [s for s in R.seasons_from("%s:%s" % (R.FIRST, R.current_season()))])
        for season in seasons:
            rows = []
            for window, span in (("1y", [season]), ("3y", [R.prev(season, 2), R.prev(season), season])):
                span = [s for s in span if s >= R.FIRST]
                paths = [p for p in (R.stint_file(s, s3, bucket, tmp, False) for s in span) if p]
                if not paths:
                    continue
                obs, _ = R.load_obs(paths)
                if len(obs) < 2000:
                    continue
                res, m = P.fit(obs, priors(span, model, s3, bucket, tmp, ours_to_nba))
                print("  %s %-2s  %6d obs · lambda %5d · prior weight k %.2f · out-of-sample error %.2f%% below plain RAPM"
                      % (season, window, m["obs"], m["lambda"], m["k"], m["cv_gain_pct"]))
                for r in res:
                    r = {(k.replace("prapm", "tprapm") if "prapm" in k else k): v for k, v in r.items()}
                    rows.append(dict(r, season=season, window=window, player_id=ids.get(r["nba_id"]),
                                     full_name=names.get(ids.get(r["nba_id"])), lam=m["lambda"], k=m["k"],
                                     seasons=",".join(span)))
            if not rows:
                continue
            if a.cmd == "check":
                for w in ("1y", "3y"):
                    P.show(rows, w, key="tprapm", title="tracking-prior RAPM (prior = box + tracking)")
                continue
            p = os.path.join(tmp, "tprapm_%s.parquet" % season)
            R.write(rows, p)
            s3.upload_file(p, bucket, "v2/models/tprapm/%s.parquet" % season)
        if a.cmd == "build":
            P.combine(s3, bucket, tmp, "tprapm")


if __name__ == "__main__":
    main()
