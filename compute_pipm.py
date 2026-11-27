#!/usr/bin/env python3
"""
compute_pipm.py — a PIPM-style rating: box score plus luck-adjusted on/off.

Jacob Goldstein's PIPM pairs a box-score estimate with a player's on/off,
the on/off made less noisy by taking the luck out of it and trusted more as
his minutes on AND off the floor grow. This is our version of that idea, with
every weight learned from our own data rather than borrowed:

  luck      each segment's points are re-counted with both teams' threes and
            free throws made at the league's rates — the part of on/off a
            player has least to do with
  on/off    offence: team offensive rating with him on minus off; defence:
            opponents' rating with him off minus on (positive is good)
  trust     each on/off is multiplied by h / (h + H), where h = on x off / (on +
            off) minutes — near zero for a player who barely sits or barely
            plays — and H is chosen by five-fold cross-validation
  weights   offence and defence fitted separately: plain three-year RAPM
            regressed on per-36 box rates plus the trusted on/off, over every
            three-season span (1,500+ minutes) since lineups begin

A season's rating applies those weights to that season alone, shrunk toward
replacement level over the first 300 minutes, as the box-prior RAPM priors
are. The box part and the on/off part are written separately, so the page can
show how far his on/off moved him.

    python3 compute_pipm.py check --season 2024-25
    python3 compute_pipm.py build                        # every lineup season
    python3 compute_pipm.py build --seasons 2026-27      # the nightly step

Writes v2/models/pipm.parquet: player_id, season, season_type ('regular'),
mp, o_pipm, d_pipm, pipm, pipm_box, pipm_oo, oo_o, oo_d (raw luck-adjusted
on/off), oo_trust. Seasons not rebuilt keep their rows.
"""
import argparse
import os
import sys
import tempfile
import zlib

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import compute_rapm as R          # noqa: E402
import compute_prior_rapm as P    # noqa: E402
import compute_tprapm as T        # noqa: E402

H_GRID = [250, 500, 1000, 2000, 4000, 8000]
SIDES = ("t", "o")
K = ("pts", "poss", "fg3m", "fg3a", "ftm", "fta")


def onoff_season(s, s3, bucket, tmp):
    """{player_id: {on_t_pts: ...}} regular season, teams summed; plus league rates."""
    import duckdb
    p = os.path.join(tmp, "oo_%s.parquet" % s)
    if not os.path.exists(p):
        try:
            s3.download_file(bucket, "v2/lineups/%s_onoff.parquet" % s, p)
        except Exception:
            return {}, None
    cols = ["on_min", "off_min"] + ["%s_%s_%s" % (w, x, k) for w in ("on", "off") for x in SIDES for k in K]
    con = duckdb.connect()
    rows = con.execute("SELECT player_id, %s FROM read_parquet(?) WHERE season_type = 'regular' "
                       "AND player_id <> '_league' GROUP BY 1"
                       % ", ".join("SUM(%s)" % c for c in cols), [p]).fetchall()
    lg = con.execute("SELECT SUM(on_t_fg3m + on_o_fg3m) / SUM(on_t_fg3a + on_o_fg3a), "
                     "SUM(on_t_ftm + on_o_ftm) / SUM(on_t_fta + on_o_fta) FROM read_parquet(?) "
                     "WHERE season_type = 'regular' AND player_id = '_league'", [p]).fetchone()
    return {r[0]: dict(zip(cols, [float(x or 0) for x in r[1:]])) for r in rows}, (float(lg[0] or .36), float(lg[1] or .77))


def add(a, b):
    for k, v in b.items():
        a[k] = a.get(k, 0.0) + v
    return a


def luck_onoff(d, lg):
    """(offence on/off, defence on/off, h) per 100, threes and free throws at league rates."""
    l3, lft = lg

    def rtg(w, x):
        pts = (d["%s_%s_pts" % (w, x)] - 3 * d["%s_%s_fg3m" % (w, x)] + 3 * l3 * d["%s_%s_fg3a" % (w, x)]
               - d["%s_%s_ftm" % (w, x)] + lft * d["%s_%s_fta" % (w, x)])
        ps = d["%s_%s_poss" % (w, x)]
        return 100.0 * pts / ps if ps > 0 else None
    on_t, off_t, on_o, off_o = rtg("on", "t"), rtg("off", "t"), rtg("on", "o"), rtg("off", "o")
    a, b = d.get("on_min", 0), d.get("off_min", 0)
    h = a * b / (a + b) if a > 0 and b > 0 else 0.0
    if None in (on_t, off_t, on_o, off_o):
        return 0.0, 0.0, 0.0
    return on_t - off_t, off_o - on_o, h


def row_features(box, oo, H):
    f = T.features(box, [])
    if f is None:
        return None
    o, dd, h = oo
    tr = h / (h + H)
    return f + [o * tr, dd * tr]


def wridge(X, y, w, pen=25.0):
    Z = np.hstack([np.ones((len(X), 1)), X])
    P_ = np.eye(Z.shape[1]) * pen
    P_[0, 0] = 0
    return np.linalg.solve(Z.T @ (Z * w[:, None]) + P_, Z.T @ (y * w))


def r2(y, f, w):
    return 1 - np.sum(w * (y - f) ** 2) / np.sum(w * (y - np.average(y, weights=w)) ** 2)


def learn(s3, bucket, tmp):
    import duckdb
    p = os.path.join(tmp, "rapm_all.parquet")
    s3.download_file(bucket, "v2/models/rapm.parquet", p)
    tgt = {(pid, s): (float(o), float(d)) for pid, s, o, d in duckdb.connect().execute(
        'SELECT player_id, season, o_rapm, d_rapm FROM read_parquet(?) WHERE "window" = \'3y\' '
        "AND player_id IS NOT NULL", [p]).fetchall()}
    seasons = R.seasons_from("%s:%s" % (R.FIRST, R.current_season()))
    box = {s: T.season_table(s, s3, bucket, tmp) for s in seasons}
    oos = {s: onoff_season(s, s3, bucket, tmp) for s in seasons}
    spans = []
    for s in seasons:
        span = [x for x in (R.prev(s, 2), R.prev(s), s) if x in box and oos[x][1]]
        if len(span) < 3:
            continue
        for pid in set().union(*[box[x].keys() for x in span]):
            if (pid, s) not in tgt:
                continue
            b, o = {}, {}
            for x in span:
                add(b, box[x].get(pid, {}))
                add(o, oos[x][0].get(pid, {}))
            if b.get("mp", 0) < 1500 or not o:
                continue
            lg = tuple(np.mean([oos[x][1][i] for x in span]) for i in (0, 1))
            spans.append((pid, b, luck_onoff(o, lg), tgt[(pid, s)]))
    if not spans:
        sys.exit("  no three-season spans with on/off and RAPM — run build_onoff.py and compute_rapm.py first")
    w = np.array([b["mp"] for _, b, _, _ in spans])
    yo = np.array([t[0] for *_, t in spans])
    yd = np.array([t[1] for *_, t in spans])
    fold = np.array([zlib.crc32(pid.encode()) % 5 for pid, *_ in spans])

    def design(H):
        X = np.array([row_features(b, oo, H) for _, b, oo, _ in spans])
        return X

    def cv(H):
        X = design(H)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Xs = (X - mu) / sd
        err = 0.0
        for k in range(5):
            tr, te = fold != k, fold == k
            for y in (yo, yd):
                c = wridge(Xs[tr], y[tr], w[tr])
                f = c[0] + Xs[te] @ c[1:]
                err += np.sum(w[te] * (y[te] - f) ** 2)
        return err
    H = min(H_GRID, key=cv)
    X = design(H)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xs = (X - mu) / sd
    co, cd = wridge(Xs, yo, w), wridge(Xs, yd, w)
    nb = Xs.shape[1] - 2
    cob, cdb = wridge(Xs[:, :nb], yo, w), wridge(Xs[:, :nb], yd, w)
    print("  weights learned on %d three-season spans (against plain RAPM); on/off trust H = %d minutes" % (len(spans), H))
    print("    offence R² %.2f (box only %.2f) · defence R² %.2f (box only %.2f)" % (
        r2(yo, co[0] + Xs @ co[1:], w), r2(yo, cob[0] + Xs[:, :nb] @ cob[1:], w),
        r2(yd, cd[0] + Xs @ cd[1:], w), r2(yd, cdb[0] + Xs[:, :nb] @ cdb[1:], w)))
    print("    per point of trusted on/off: offence %+.3f, defence %+.3f" % (co[-2] / sd[-2], cd[-1] / sd[-1]))
    return {"H": H, "mu": mu, "sd": sd, "co": co, "cd": cd, "box": box, "oos": oos}


def rate_season(s, m, s3, bucket, tmp):
    box = m["box"].get(s) or T.season_table(s, s3, bucket, tmp)
    oo, lg = m["oos"].get(s) or onoff_season(s, s3, bucket, tmp)
    if not lg:
        return []
    out = []
    for pid, b in box.items():
        mp = b.get("mp", 0)
        o_raw = luck_onoff(oo[pid], lg) if pid in oo else (0.0, 0.0, 0.0)
        f = row_features(b, o_raw, m["H"])
        if f is None:
            continue
        z = (np.array(f) - m["mu"]) / m["sd"]
        z0 = z.copy()
        z0[-2:] = (0 - m["mu"][-2:]) / m["sd"][-2:]          # on/off at zero: the box part alone
        o, dd = float(m["co"][0] + z @ m["co"][1:]), float(m["cd"][0] + z @ m["cd"][1:])
        ob, db = float(m["co"][0] + z0 @ m["co"][1:]), float(m["cd"][0] + z0 @ m["cd"][1:])
        sh = lambda v, repl: (v * mp + P.SHRINK_MIN * repl) / (mp + P.SHRINK_MIN)
        o, dd, ob, db = sh(o, P.REPL_O), sh(dd, P.REPL_D), sh(ob, P.REPL_O), sh(db, P.REPL_D)
        h = o_raw[2]
        out.append({"player_id": pid, "season": s, "season_type": "regular", "mp": mp,
                    "o_pipm": o, "d_pipm": dd, "pipm": o + dd, "pipm_box": ob + db,
                    "pipm_oo": (o + dd) - (ob + db), "oo_o": o_raw[0] if pid in oo else None,
                    "oo_d": o_raw[1] if pid in oo else None, "oo_trust": h / (h + m["H"])})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    a = ap.parse_args()
    import duckdb
    spec = a.seasons or a.season or "%s:%s" % (R.FIRST, R.current_season())
    seasons = [s for s in R.seasons_from(spec) if s >= R.FIRST]
    s3, bucket = R.r2()
    names = R.names_map()
    with tempfile.TemporaryDirectory() as tmp:
        m = learn(s3, bucket, tmp)
        rows = []
        for s in seasons:
            got = rate_season(s, m, s3, bucket, tmp)
            top = sorted([r for r in got if r["mp"] >= 1000], key=lambda r: -r["pipm"])
            print("  %s: %d players; top: %s" % (s, len(got), "; ".join(
                "%s %+.1f" % (names.get(r["player_id"], r["player_id"]), r["pipm"]) for r in top[:3])))
            if a.cmd == "check":
                print("      %-26s %6s %6s %6s   %6s %6s  %5s %6s" % ("", "PIPM", "O", "D", "box", "on/off", "trust", "min"))
                for r in top[:20]:
                    print("      %-26s %+6.2f %+6.2f %+6.2f   %+6.2f %+6.2f  %5.2f %6.0f" % (
                        (names.get(r["player_id"]) or r["player_id"])[:26], r["pipm"], r["o_pipm"], r["d_pipm"],
                        r["pipm_box"], r["pipm_oo"], r["oo_trust"], r["mp"]))
            rows += got
        if a.cmd == "check" or not rows:
            return
        p = os.path.join(tmp, "new.parquet")
        R.write(rows, p)
        old = os.path.join(tmp, "old.parquet")
        keep = ""
        try:
            s3.download_file(bucket, "v2/models/pipm.parquet", old)
            keep = (" UNION ALL BY NAME SELECT * FROM read_parquet('%s') WHERE season NOT IN (%s)"
                    % (old, ", ".join("'%s'" % s for s in seasons)))
        except Exception:
            pass
        out = os.path.join(tmp, "pipm.parquet")
        duckdb.connect().execute("COPY (SELECT * FROM read_parquet('%s')%s) TO '%s' (FORMAT PARQUET)" % (p, keep, out))
        s3.upload_file(out, bucket, "v2/models/pipm.parquet")
        print("  v2/models/pipm.parquet written")


if __name__ == "__main__":
    main()
