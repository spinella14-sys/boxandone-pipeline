#!/usr/bin/env python3
"""
patch_rapm_factors2.py — run once.

The factor split leaves out any game whose stint tallies don't add up to the
box score (build_lineups' factors_match, in {season}_games.parquet). Those
games stay in RAPM itself, where only points and possessions count.

It also refuses to publish a split it can't trust. If the factors explain less
than 80% of team-game scoring (R² under 0.80; every clean season so far fits
at 0.91–0.93), that window keeps RAPM and leaves the factor columns blank, and
the run says so. That is what 2019-20 did at R² 0.20. Because 2019-20 sits in
the three-year windows, it also dragged 2020-21 down to 0.34 and 2021-22 to
0.44.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "compute_rapm.py")
src = open(PATH).read()
if "def bad_tally_games" in src:
    print("  compute_rapm.py already patched")
    sys.exit()

PAIRS = [
    # which games' tallies are off
    ('''def load_obs(paths):''', '''def bad_tally_games(season, s3, bucket, tmp):
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


def load_obs(paths, bad=frozenset()):''', 1),
    ('''    parallel to obs as (offence's, defence's) dicts, or is None when any file
    predates the per-stint box score."""''', '''    parallel to obs as (offence's, defence's) dicts, or is None when any file
    predates the per-stint box score. A game in `bad` gets None entries: it
    counts for RAPM but not for the factor split."""''', 1),
    ('''                if tal is not None:
                    tal.append((ht, at))''', '''                if tal is not None:
                    tal.append(None if gid in bad else (ht, at))''', 1),
    ('''                if tal is not None:
                    tal.append((at, ht))''', '''                if tal is not None:
                    tal.append(None if gid in bad else (at, ht))''', 1),
    # skip the left-out games in both factor steps
    ('''        vals = [f(o, d, ob[4]) for ob, (o, d) in zip(obs, tal)]''',
     '''        vals = [f(t[0], t[1], ob[4]) if t else (0.0, 0.0) for ob, t in zip(obs, tal)]''', 1),
    ('''    for ob, (o, d) in zip(obs, tal):
        k = (ob[6], ob[2])''', '''    for ob, t in zip(obs, tal):
        if t is None:
            continue
        o, d = t
        k = (ob[6], ob[2])''', 1),
    # the trust gate
    ('''        worth = factor_worth(obs, tal)
        if worth:''', '''        worth = factor_worth(obs, tal)
        if worth and worth["r2"] < MIN_FACTOR_R2:
            skipped = worth["r2"]
            worth = None
        else:
            skipped = None
        if worth:''', 1),
    ('''    factors, worth = {}, None
    if tal is not None:''', '''    factors, worth, skipped = {}, None, None
    if tal is not None:''', 1),
    ('''        meta["worth"] = {k: round(v, 2) for k, v in worth.items()}
    return out, meta''', '''        meta["worth"] = {k: round(v, 2) for k, v in worth.items()}
    if skipped is not None:
        meta["factors_skipped_r2"] = round(skipped, 2)
    if tal is not None:
        meta["factor_games_left_out"] = len({ob[6] for ob, t in zip(obs, tal) if t is None})
    return out, meta''', 1),
    # run_season: collect the bad games across the window, report
    ('''        obs, tal = load_obs(paths)''', '''        bad = set()
        for s in span:
            bad |= bad_tally_games(s, s3, bucket, tmp)
        obs, tal = load_obs(paths, bad)''', 1),
    ('''        else:
            print("            factors: not in these lineup files — rebuild lineups for the split")''',
     '''        elif m.get("factors_skipped_r2") is not None:
            print("            factors: NOT published — they explain only R² %.2f of team-game scoring"
                  % m["factors_skipped_r2"])
        else:
            print("            factors: not in these lineup files — rebuild lineups for the split")
        if m.get("factor_games_left_out"):
            print("            (%d games left out of the split: tallies don't match the box score)"
                  % m["factor_games_left_out"])''', 1),
]

for old, new, count in PAIRS:
    n = src.count(old)
    if n != count:
        sys.exit("  compute_rapm.py not as expected (%d× %r) — nothing written" % (n, old[:70]))
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  compute_rapm.py patched")
