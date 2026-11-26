#!/usr/bin/env python3
"""
compute_dpm.py — DPM, Box and One's daily plus-minus.

The idea is DARKO's (Kostya Medvedovsky): a player's value, re-estimated
after every game, with recent games counting more than old ones, so the line
moves when a player changes and settles when he doesn't. The machinery is our
own, built on what we already have:

  1. a box-score estimate (SPM) — per-36 box rates mapped to points per 100
     by a regression fitted on our box-prior RAPM (compute_prior_rapm.py), so
     the box score is weighted by what our lineup model learned it's worth
  2. a running, time-decayed ridge regression over every stint since 2019-20,
     pulled toward each player's SPM (from his time-decayed box score) rather
     than toward zero — box-prior RAPM, continuously

Decay. Both halves forget at a steady rate per calendar day, offseason
included. Plus-minus: a game a year old counts half as much as last night's
(DARKO's pace for its plus-minus component). Box score: half-life of 200 days
— box rates stabilise faster than lineup data, so they are allowed to move
faster, as in DARKO. Both are constants below, to be tuned by the scorecard.

Because the decay is exponential, the regression's normal equations can be
carried forward a day at a time (multiply by the day's decay, add the day's
stints) and re-solved — no refit from scratch.

Output: one row per player per game he played (regular season), his DPM as it
stood after that game.

    python3 compute_dpm.py check                 # fit, print the latest leaderboard, write nothing
    python3 compute_dpm.py build                 # everything since 2019-20 -> R2
    python3 compute_dpm.py build --recent        # the nightly: player files only for recent games

Writes
  v2/models/dpm.parquet                    every player-game rating
  v2/models/dpm_season.parquet             per player and season: where he ended it,
                                           and his minutes-weighted average across it
  v2/players/{player_id}/dpm.parquet       his own line, for the Models tab chart
                                           (build writes everyone's; the nightly, with
                                           --recent, only players who played lately)
"""
import argparse
import os
import sys
import tempfile
from datetime import date

import numpy as np

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import compute_rapm as R  # noqa: E402

HALF_LIFE_PM = 365.0     # days: plus-minus (DARKO's pace)
HALF_LIFE_BOX = 200.0    # days: box score
LAMBDA = 5000.0          # ridge penalty (box-prior RAPM chose 5,000-8,000 for one season)
K_PRIOR = 1.0            # trust in the box-score prior
SHRINK_MIN = 250.0       # decayed minutes of replacement level mixed into the box estimate
REPL_O, REPL_D = -1.5, -0.5
BOX = ("pts", "fga", "fg3a", "fg3m", "fta", "ftm", "orb", "drb", "ast", "stl", "blk", "tov", "pf")


def seasons_all():
    return [s for s in R.seasons_from("%s:%s" % (R.FIRST, R.current_season()))]


def load_box(s3, bucket, tmp, seasons):
    """Regular-season player-game box rows, in date order."""
    import duckdb
    con = duckdb.connect()
    paths = []
    for s in seasons:
        p = os.path.join(tmp, "pg_%s.parquet" % s)
        try:
            s3.download_file(bucket, "v2/seasons/%s/player_game.parquet" % s, p)
            paths.append(p)
        except Exception:
            print("  %s: no player_game file" % s)
    rows = con.execute("""
        SELECT player_id, game_id, CAST(game_date AS DATE), season, COALESCE(mp, 0), %s
        FROM read_parquet(%s, union_by_name=true)
        WHERE season_type = 'regular' AND mp > 0 ORDER BY 3""" % (
        ", ".join("COALESCE(%s, 0)" % k for k in BOX), repr(paths))).fetchall()
    return rows


def spm_fit(s3, bucket, tmp, box_rows):
    """Coefficients mapping per-36 box rates to O and D points per 100, fitted
    (minutes-weighted) on one-season box-prior RAPM for players with 500+ minutes."""
    import duckdb
    p = os.path.join(tmp, "prapm.parquet")
    s3.download_file(bucket, "v2/models/prapm.parquet", p)
    target = {(pid, s): (float(o or 0), float(d or 0)) for pid, s, o, d in duckdb.connect().execute(
        "SELECT player_id, season, o_prapm, d_prapm FROM read_parquet(?) WHERE \"window\" = '1y' "
        "AND player_id IS NOT NULL", [p]).fetchall()}
    agg = {}
    for r in box_rows:
        k = (r[0], r[3])
        a = agg.setdefault(k, np.zeros(1 + len(BOX)))
        a[0] += r[4]
        a[1:] += np.array(r[5:], dtype=float)
    X, yo, yd, w = [], [], [], []
    for k, a in agg.items():
        if a[0] < 500 or k not in target:
            continue
        X.append(rates(a[1:], a[0]))
        yo.append(target[k][0])
        yd.append(target[k][1])
        w.append(a[0])
    X, w = np.array(X), np.sqrt(np.array(w))
    Xi = np.hstack([np.ones((len(X), 1)), X])
    pen = np.diag([0.0] + [1.0] * X.shape[1])      # a touch of ridge keeps collinear rates sane
    co = np.linalg.solve(Xi.T @ (Xi * w[:, None] ** 2) + pen, Xi.T @ (np.array(yo) * w ** 2))
    cd = np.linalg.solve(Xi.T @ (Xi * w[:, None] ** 2) + pen, Xi.T @ (np.array(yd) * w ** 2))
    fo, fd = Xi @ co, Xi @ cd
    r2 = lambda y, f: 1 - np.sum(w ** 2 * (y - f) ** 2) / np.sum(w ** 2 * (y - np.average(y, weights=w ** 2)) ** 2)
    print("  box estimate fitted on %d player-seasons: offence R² %.2f, defence R² %.2f"
          % (len(X), r2(np.array(yo), fo), r2(np.array(yd), fd)))
    return co, cd


def game_dates(s3, bucket, tmp, nba_ids, games_map, gdate):
    """{NBA game id: date} for every stint game. Kept on R2 as
    v2/lineups/game_dates.parquet, because older games' dates come from NBA's
    own box score (its gameCode), which only the machine holding the
    play-by-play cache can read; GitHub's nightly reads the saved map."""
    import duckdb
    from datetime import date as D
    p = os.path.join(tmp, "game_dates.parquet")
    have = {}
    try:
        s3.download_file(bucket, "v2/lineups/game_dates.parquet", p)
        have = {g: d for g, d in duckdb.connect().execute(
            "SELECT game_id, CAST(game_date AS DATE) FROM read_parquet(?)", [p]).fetchall()}
    except Exception:
        pass
    added = 0
    for g in nba_ids:
        if g in have:
            continue
        d = gdate.get(games_map.get(g))
        if d is None:
            try:
                import cdn_feed as C
                code = (((C.load(g, "box", fetch=False) or {}).get("game") or {}).get("gameCode") or "")
                if len(code) >= 8 and code[:8].isdigit():
                    d = D(int(code[:4]), int(code[4:6]), int(code[6:8]))
            except Exception:
                d = None
        if d is not None:
            have[g] = d
            added += 1
    if added:
        c = duckdb.connect()
        c.execute("CREATE TABLE t (game_id VARCHAR, game_date DATE)")
        c.executemany("INSERT INTO t VALUES (?, ?)", sorted(have.items()))
        c.execute("COPY t TO '%s' (FORMAT PARQUET)" % p)
        s3.upload_file(p, bucket, "v2/lineups/game_dates.parquet")
    missing = len(set(nba_ids) - set(have))
    print("  game dates: %d known (%d new)%s" % (len(have), added,
          ", %d stint games without a date — left out" % missing if missing else ""))
    return have


def rates(sums, mp):
    """Per-36 rates, plus true shooting and three-point rate."""
    v = dict(zip(BOX, sums))
    per = 36.0 / mp if mp else 0.0
    tsa = v["fga"] + 0.44 * v["fta"]
    return [v[k] * per for k in BOX] + [v["pts"] / (2 * tsa) if tsa else 0.5,
                                         v["fg3a"] / v["fga"] if v["fga"] else 0.0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--recent", action="store_true", help="player files only for those who played in the last 4 days")
    a = ap.parse_args()
    import duckdb
    s3, bucket = R.r2()
    ids = R.nba_to_ours()                # NBA person id -> ours
    import export_game_lineups as X
    games_map, _, _ = X.maps()           # NBA game id -> ours
    names = R.names_map()
    seasons = seasons_all()
    with tempfile.TemporaryDirectory() as tmp:
        box = load_box(s3, bucket, tmp, seasons)
        co, cd = spm_fit(s3, bucket, tmp, box)
        gdate = {}
        for r in box:
            gdate.setdefault(r[1], r[2])

        # stints, by date
        by_day = {}
        means = {}
        loaded = []
        for s in seasons:
            p = R.stint_file(s, s3, bucket, tmp, False)
            if not p:
                continue
            obs, _ = R.load_obs([p])
            tot = sum(100.0 * o[3] for o in obs)
            poss = sum(o[4] for o in obs)
            means[s] = tot / poss if poss else 110.0
            loaded.append((s, obs))
        dates = game_dates(s3, bucket, tmp, sorted({o[6] for _, obs in loaded for o in obs}), games_map, gdate)
        for s, obs in loaded:
            for o in obs:
                d = dates.get(o[6])
                if d is None:
                    continue
                by_day.setdefault(d, []).append((o, s))
        days = sorted(set(by_day) | {r[2] for r in box})
        print("  %d game days, %d stint observations"
              % (len(days), sum(len(v) for v in by_day.values())))

        players = sorted({ours for ours in ids.values()} & {r[0] for r in box})
        nba_of = {v: k for k, v in ids.items()}
        index = {pid: i for i, pid in enumerate(players)}
        n_p = len(players)
        n = 2 * n_p + 1
        A = np.zeros((n, n))
        b = np.zeros(n)
        bsum = np.zeros((n_p, len(BOX)))
        bmin = np.zeros(n_p)
        box_by_day = {}
        for r in box:
            box_by_day.setdefault(r[2], []).append(r)
        dpm_pm = 0.5 ** (1.0 / HALF_LIFE_PM)
        dbox = 0.5 ** (1.0 / HALF_LIFE_BOX)
        out, prev = [], None
        for i, d in enumerate(days):
            gap = (d - prev).days if prev else 0
            prev = d
            if gap:
                A *= dpm_pm ** gap
                b *= dpm_pm ** gap
                bsum *= dbox ** gap
                bmin *= dbox ** gap
            # the day's stints into the normal equations
            for o, s in by_day.get(d, []):
                off, de, home, pts, poss = o[0], o[1], o[2], o[3], o[4]
                cols = [index.get(ids.get(p)) for p in off] + [None if index.get(ids.get(p)) is None
                                                                else n_p + index[ids[p]] for p in de]
                cols = [c for c in cols if c is not None] + [n - 1]
                vals = np.ones(len(cols))
                vals[-1] = home
                y = 100.0 * pts / poss - means[s]
                for c1, v1 in zip(cols, vals):
                    b[c1] += poss * v1 * y
                    for c2, v2 in zip(cols, vals):
                        A[c1, c2] += poss * v1 * v2
            # the day's box scores into the decayed box totals
            played = []
            for r in box_by_day.get(d, []):
                j = index.get(r[0])
                if j is None:
                    continue
                bsum[j] += np.array(r[5:], dtype=float)
                bmin[j] += r[4]
                played.append((j, r))
            if not played:
                continue
            # priors from the decayed box score, shrunk toward replacement
            Xb = np.array([rates(bsum[j], bmin[j]) if bmin[j] > 0 else rates(np.zeros(len(BOX)), 1)
                           for j in range(n_p)])
            Xi = np.hstack([np.ones((n_p, 1)), Xb])
            so, sd = Xi @ co, Xi @ cd
            wt = bmin / (bmin + SHRINK_MIN)
            po = wt * so + (1 - wt) * REPL_O
            pd = wt * sd + (1 - wt) * REPL_D
            p0 = np.concatenate([po, -pd, [0.0]])
            pen = np.full(n, LAMBDA)
            pen[-1] = 1.0
            coef = np.linalg.solve(A + np.diag(pen), b + pen * K_PRIOR * p0)
            season = played[0][1][3]
            for j, r in played:
                o, dd = coef[j], -coef[n_p + j]
                out.append((players[j], d, season, round(float(o + dd), 3), round(float(o), 3),
                            round(float(dd), 3), round(float(po[j] + pd[j]), 3), round(float(bmin[j]), 1)))
            if (i + 1) % 100 == 0:
                print("    %s  %d/%d days" % (d, i + 1, len(days)), end="\r")
        print("    done: %d player-game ratings                 " % len(out))

        # the latest value per player, for a look
        last = {}
        for r in out:
            last[r[0]] = r
        cur = [r for r in last.values() if r[2] == seasons[-1] and r[7] >= 400]
        cur.sort(key=lambda r: -r[3])
        print("\n  DPM as of each player's latest game (%s, 400+ decayed minutes)" % seasons[-1])
        print("    %-24s %6s  %6s %6s   %6s" % ("", "DPM", "O", "D", "box"))
        for r in cur[:20]:
            print("    %-24s %+6.2f  %+6.2f %+6.2f   %+6.2f" % ((names.get(r[0]) or r[0])[:24], r[3], r[4], r[5], r[6]))
        if a.cmd == "build":
            path = os.path.join(tmp, "dpm.parquet")
            jl = path + ".csv"
            with open(jl, "w") as f:
                f.write("player_id,date,season,dpm,o_dpm,d_dpm,box,box_minutes\n")
                for r in out:
                    f.write("%s,%s,%s,%s,%s,%s,%s,%s\n" % r)
            duckdb.connect().execute("COPY (SELECT * FROM read_csv_auto('%s', header=true)) TO '%s' "
                                     "(FORMAT PARQUET, COMPRESSION ZSTD)" % (jl, path))
            s3.upload_file(path, bucket, "v2/models/dpm.parquet")
            print("\n  v2/models/dpm.parquet: %d rows" % len(out))
            c = duckdb.connect()
            c.execute("CREATE TABLE d AS SELECT * FROM read_parquet(?)", [path])
            sp = os.path.join(tmp, "dpm_season.parquet")
            c.execute("""COPY (
                SELECT player_id, season,
                       arg_max(dpm, date) AS dpm_end, arg_max(o_dpm, date) AS o_dpm_end,
                       arg_max(d_dpm, date) AS d_dpm_end, avg(dpm) AS dpm_avg, count(*) AS games
                FROM d GROUP BY 1, 2) TO '%s' (FORMAT PARQUET)""" % sp)
            s3.upload_file(sp, bucket, "v2/models/dpm_season.parquet")
            print("  v2/models/dpm_season.parquet written")
            latest = max(r[1] for r in out)
            who = sorted({r[0] for r in out if not a.recent or (latest - r[1]).days <= 4})
            pp = os.path.join(tmp, "one.parquet")
            for i, pid in enumerate(who):
                c.execute("COPY (SELECT date, season, dpm, o_dpm, d_dpm, box FROM d WHERE player_id = ? "
                          "ORDER BY date) TO '%s' (FORMAT PARQUET)" % pp, [pid])
                s3.upload_file(pp, bucket, "v2/players/%s/dpm.parquet" % pid)
                if (i + 1) % 100 == 0:
                    print("    player files %d/%d" % (i + 1, len(who)), end="\r")
            print("  player files: %d written            " % len(who))


if __name__ == "__main__":
    main()
