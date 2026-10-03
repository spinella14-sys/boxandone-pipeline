#!/usr/bin/env python3
"""
tov_vs_usage.py — does turnover rate rise with usage?

xTRAPoss charges a player for turnovers above expectation. Expectation is
currently the league's flat turnover rate, which assumes a 32%-usage creator
and a 13%-usage spot-up shooter should turn it over at the same rate per play.
That is an assumption, and it is testable.

If the slope is meaningfully positive, high-usage players turn it over more per
play as a matter of role rather than carelessness, and the flat baseline
over-charges them. The fix is a usage-dependent baseline:

    expected TOV% = intercept + slope x USG%

If the slope is near zero, the flat baseline is already right and nothing
changes.

    python3 tov_vs_usage.py                 # most recent season
    python3 tov_vs_usage.py --season 2015-16
    python3 tov_vs_usage.py --all           # every season, to see if it drifts

Reads the exported season files; writes nothing.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
MIN_MP = 500


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def season_list(s3, bucket):
    out, token = set(), None
    while True:
        kw = {"Bucket": bucket, "Prefix": "v2/seasons/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            p = o["Key"].split("/")
            if len(p) >= 3:
                out.add(p[2])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return sorted(out, reverse=True)


def fetch(env, season):
    """Player usage and turnover rate for one season, weighted by plays."""
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        ps = os.path.join(tmp, "p.parquet")
        ts = os.path.join(tmp, "t.parquet")
        s3.download_file(bucket, "v2/seasons/%s/player_season.parquet" % season, ps)
        s3.download_file(bucket, "v2/seasons/%s/team_season.parquet" % season, ts)
        rows = con.execute("""
            SELECT p.full_name,
                   p.mp,
                   (p.fga + 0.44*p.fta + p.tov)                      AS plays,
                   p.tov,
                   100.0 * ((p.fga + 0.44*p.fta + p.tov) * (t.team_mp/5.0))
                     / NULLIF(p.mp * (t.fga + 0.44*t.fta + t.tov), 0) AS usg
            FROM read_parquet('%s') p
            JOIN read_parquet('%s') t
              ON t.team_abbr = p.last_team AND t.season_type = p.season_type
            WHERE p.season_type = 'regular' AND p.mp >= %d
              AND (p.fga + 0.44*p.fta + p.tov) > 0
        """ % (ps, ts, MIN_MP)).fetchall()
    con.close()
    out = []
    for name, mp, plays, tov, usg in rows:
        if usg is None or plays is None or not plays:
            continue
        out.append((name, float(usg), float(tov) / float(plays), float(plays)))
    return out


def regress(points):
    """Weighted least squares of TOV% on USG%, weighted by plays — a player
    with 1,500 plays should count for more than one with 300."""
    sw = sum(w for _, _, _, w in points)
    if sw <= 0:
        return None
    mx = sum(x * w for _, x, _, w in points) / sw
    my = sum(y * w for _, _, y, w in points) / sw
    sxx = sum(w * (x - mx) ** 2 for _, x, _, w in points)
    sxy = sum(w * (x - mx) * (y - my) for _, x, y, w in points)
    if sxx == 0:
        return None
    slope = sxy / sxx
    intercept = my - slope * mx
    syy = sum(w * (y - my) ** 2 for _, _, y, w in points)
    r2 = (sxy ** 2) / (sxx * syy) if syy else 0.0
    return slope, intercept, r2, mx, my


def report(season, points):
    res = regress(points)
    if not res:
        print("  %s: not enough data" % season)
        return None
    slope, intercept, r2, mx, my = res
    print("  %-9s n=%4d  mean USG %.1f%%  mean TOV%% %.1f%%  "
          "slope %+.5f  R2 %.3f" % (season, len(points), mx, my * 100, slope, r2))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    env = load_env()
    s3 = s3c(env)
    seasons = season_list(s3, env["R2_BUCKET_NAME"])
    targets = seasons if a.all else [a.season or seasons[0]]

    print("  turnover rate against usage, players with %d+ minutes" % MIN_MP)
    print("  (weighted by plays, so volume counts)\n")
    last = None
    for s in targets:
        try:
            pts = fetch(env, s)
        except Exception as e:
            print("  %-9s unavailable (%s)" % (s, str(e)[:40]))
            continue
        res = report(s, pts)
        if res:
            last = (s, pts, res)

    if not last:
        return
    season, points, (slope, intercept, r2, mx, my) = last
    print("\n  for %s:" % season)
    print("    expected TOV%% = %.4f + %.5f x USG%%" % (intercept, slope))
    print()
    print("    at 13%% usage: %.1f%%   at 22%%: %.1f%%   at 32%%: %.1f%%"
          % (100 * (intercept + slope * 13), 100 * (intercept + slope * 22),
             100 * (intercept + slope * 32)))
    print("    flat league baseline: %.1f%%" % (100 * my))
    spread = 100 * slope * (32 - 13)
    print()
    print("    across the usage range the expectation moves %.2f points." % spread)
    if abs(spread) < 0.5:
        print("    That is small — the flat baseline is fine and nothing needs changing.")
    elif abs(spread) < 1.5:
        print("    Modest. A usage-dependent baseline would shift high-usage")
        print("    players by a few tenths of an opportunity per 100.")
    else:
        print("    Substantial. The flat baseline over-charges high-usage players")
        print("    and a usage-dependent one is worth adopting.")

    # what it would do to the extremes
    print("\n    effect on the players it matters most for:")
    ranked = sorted(points, key=lambda p: -p[1])
    for label, group in (("highest usage", ranked[:5]), ("lowest usage", ranked[-5:])):
        print("      %s:" % label)
        for name, usg, tovr, plays in group:
            flat = (tovr - my) * plays
            fitted = (tovr - (intercept + slope * usg)) * plays
            print("        %-24s USG %4.1f  TOV%% %4.1f  charge: flat %+6.1f  fitted %+6.1f"
                  % (name[:24], usg, 100 * tovr, flat, fitted))


if __name__ == "__main__":
    main()
