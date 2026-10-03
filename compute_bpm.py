#!/usr/bin/env python3
"""
compute_bpm.py — Box Plus/Minus 2.0, OBPM, DBPM and VORP.

Daniel Myers' published method (basketball-reference.com/about/bpm2.html),
implemented against our own box scores. The sequence:

  1. estimate position and offensive role from the player's share of team
     statistics, clamp to [1, 5], then shift each team so its minute-weighted
     average is exactly 3.0 — iteratively, because the clamp fights the shift
  2. interpolate the regression coefficients between position 1 and 5, and
     between offensive role 1 and 5 for the shot-attempt terms
  3. raw BPM, plus the position and offensive-role constants
  4. a team adjustment that forces the minute-weighted team sum to equal the
     team's actual efficiency margin — this is the step that redistributes the
     credit the box score cannot assign, and it is why BPM needs team context
     at all

VORP = (BPM + 2.0) * (share of team minutes) * (team games / 82), where -2.0 is
replacement level.

ONE KNOWN GAP: step 3 of the published sequence adjusts each player's points
for team shooting context, against "the baseline points per adjusted shot
attempt used by the regression". That constant is not in the published write-up
— only in the author's linked spreadsheet. PTS_BASELINE below is a calibratable
stand-in. The final team adjustment absorbs most of the error at team level, so
team sums stay right; what moves is the split between a team's high-volume and
low-volume scorers. Compare a few players against Basketball Reference and set
the constant where they line up.

    python3 compute_bpm.py --season 2025-26 --dry-run   # inspect, write nothing
    python3 compute_bpm.py                              # all seasons, rewrite
    python3 compute_bpm.py --baseline 1.09

Rewrites v2/seasons/{season}/player_season.parquet with bpm, obpm, dbpm, vorp,
pos_est and role_est added.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2"

# points per adjusted shot attempt that the regression was centred on; see the
# note above — this is the one value the published method does not state
PTS_BASELINE = 1.09

# BPM 2.0 regression. Values that vary run (position 1, position 5).
BPM_C = {
    "pts":  (0.860, 0.860),
    "fg3m": (0.389, 0.389),
    "ast":  (0.580, 1.034),
    "tov": (-0.964, -0.964),
    "orb":  (0.613, 0.181),
    "drb":  (0.116, 0.181),
    "stl":  (1.369, 1.008),
    "blk":  (1.327, 0.703),
    "pf":  (-0.367, -0.367),
}
BPM_ROLE = {"fga": (-0.560, -0.780), "fta": (-0.246, -0.343)}
BPM_POS_CONST = -0.818      # at position 1, zero at 3 and above
BPM_ROLE_CONST = -2.774     # at role 1, zero at 3, +2.774 at role 5

OBPM_C = {
    "pts":  (0.605, 0.605),
    "fg3m": (0.477, 0.477),
    "ast":  (0.476, 0.476),
    "tov": (-0.579, -0.882),
    "orb":  (0.606, 0.422),
    "drb": (-0.112, 0.103),
    "stl":  (0.177, 0.294),
    "blk":  (0.725, 0.097),
    "pf":  (-0.439, -0.439),
}
OBPM_ROLE = {"fga": (-0.330, -0.472), "fta": (-0.145, -0.208)}
OBPM_POS_CONST = -1.698
OBPM_ROLE_CONST = -0.860

POS_REG = {"int": 2.130, "trb": 8.668, "stl": -2.486, "pf": 0.992,
           "ast": -3.536, "blk": 1.667}
ROLE_REG = {"int": 6.00, "ast": -6.642, "thresh": -8.544}
THRESH_OFFSET = 0.33        # threshold efficiency sits this far below team avg


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


def lerp(pair, pos):
    """Coefficients vary linearly between position 1 and position 5."""
    lo, hi = pair
    return lo + (hi - lo) * (pos - 1.0) / 4.0


def normalise(vals, weights, target=3.0, lo=1.0, hi=5.0, rounds=12):
    """Shift so the weighted mean hits the target, clamping each round. The
    clamp pushes the mean back off target, hence the loop."""
    shift = 0.0
    tw = sum(weights) or 1.0
    for _ in range(rounds):
        clamped = [min(hi, max(lo, v + shift)) for v in vals]
        mean = sum(c * w for c, w in zip(clamped, weights)) / tw
        err = target - mean
        if abs(err) < 1e-9:
            break
        shift += err
    return [min(hi, max(lo, v + shift)) for v in vals]


def _f(v):
    """DuckDB returns Decimal for some aggregates and it will not mix with
    float arithmetic, so every numeric is coerced once on the way in."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


STR_COLS = ("team_abbr", "season", "season_type", "league", "conference",
            "division", "player_id", "full_name", "last_team")


def bpm_for_team(players, team, lg_ortg, baseline=PTS_BASELINE):
    team = {k: (v if k in STR_COLS else _f(v)) for k, v in team.items()}
    for p in players:
        for k in ("mp", "pts", "fg3m", "ast", "tov", "orb", "drb", "trb",
                  "stl", "blk", "pf", "fga", "fta", "fgm", "gp"):
            if k in p:
                p[k] = _f(p[k])
    """players: list of dicts with box totals. team: the team season row."""
    tm_mp = team["team_mp"] or 0
    if not tm_mp:
        return
    tm5 = tm_mp / 5.0
    poss = team["poss_est"] or 0
    opp_poss = team.get("opp_poss_raw") or poss
    if not poss:
        return

    # team efficiency margin per 100 possessions, which the team sum must match
    team_rtg = 100.0 * (team["pts"] / poss) - 100.0 * (team["opp_pts"] / opp_poss)

    tm = {k: (team.get(k) or 0) for k in
          ("trb", "stl", "pf", "ast", "blk", "fgm", "fga", "fta", "pts", "tov")}
    tm_tsa = tm["fga"] + 0.44 * tm["fta"]
    tm_pts_per_tsa = tm["pts"] / tm_tsa if tm_tsa else baseline

    rows = []
    for p in players:
        mp = p.get("mp") or 0
        if mp <= 0:
            continue
        share = mp / tm5                      # fraction of available minutes
        if share <= 0:
            continue
        per100 = lambda k: 100.0 * (p.get(k) or 0) / (poss * share) if poss * share else 0.0
        pct = lambda k: ((p.get(k) or 0) / (tm[k] * share)) if tm[k] and share else 0.0

        tsa = (p.get("fga") or 0) + 0.44 * (p.get("fta") or 0)
        pts_per_tsa = (p.get("pts") or 0) / tsa if tsa else 0.0
        thresh_level = tm_pts_per_tsa - THRESH_OFFSET
        thresh_pts = (pts_per_tsa - thresh_level) * tsa
        tm_thresh = tm["pts"] - thresh_level * tm_tsa
        pct_thresh = thresh_pts / (tm_thresh * share) if tm_thresh and share else 0.0

        rows.append({
            "p": p, "mp": mp, "share": share,
            "per100": {k: per100(k) for k in
                       ("pts", "fg3m", "ast", "tov", "orb", "drb", "stl", "blk",
                        "pf", "fga", "fta")},
            "tsa100": 100.0 * tsa / (poss * share) if poss * share else 0.0,
            "raw_pos": (POS_REG["int"] + POS_REG["trb"] * pct("trb")
                        + POS_REG["stl"] * pct("stl") + POS_REG["pf"] * pct("pf")
                        + POS_REG["ast"] * pct("ast") + POS_REG["blk"] * pct("blk")),
            "raw_role": (ROLE_REG["int"] + ROLE_REG["ast"] * pct("ast")
                         + ROLE_REG["thresh"] * pct_thresh),
        })
    if not rows:
        return

    w = [r["mp"] for r in rows]
    # small samples are pulled toward a neutral prior, as the method prescribes
    pos_pre = [(r["raw_pos"] * r["mp"] + 3.0 * 50.0) / (r["mp"] + 50.0) for r in rows]
    role_pre = [(r["raw_role"] * r["mp"] + 4.0 * 50.0) / (r["mp"] + 50.0) for r in rows]
    positions = normalise(pos_pre, w)
    roles = normalise(role_pre, w)

    # team shooting context: shift everyone's points by the gap between the
    # team's efficiency and the regression's baseline
    ctx = (tm_pts_per_tsa - baseline)

    for r, pos, role in zip(rows, positions, roles):
        r["pos"] = pos
        r["role"] = role
        for coeffs, role_coeffs, pos_k, role_k, out in (
                (BPM_C, BPM_ROLE, BPM_POS_CONST, BPM_ROLE_CONST, "raw_bpm"),
                (OBPM_C, OBPM_ROLE, OBPM_POS_CONST, OBPM_ROLE_CONST, "raw_obpm")):
            v = 0.0
            for k, pair in coeffs.items():
                stat = r["per100"][k]
                if k == "pts":
                    stat = stat - ctx * r["tsa100"]
                v += lerp(pair, pos) * stat
            for k, pair in role_coeffs.items():
                v += lerp(pair, role) * r["per100"][k]
            # position constant is zero above 3 and falls linearly to pos 1
            v += (3.0 - pos) * (pos_k / 2.0) if pos < 3.0 else 0.0
            v += (3.0 - role) * (role_k / 2.0)
            r[out] = v

    # team adjustment: the minute-weighted sum must equal the team's margin
    tot_share = sum(r["share"] for r in rows) or 1.0
    team_ortg = 100.0 * team["pts"] / poss
    for raw, final in (("raw_bpm", "bpm"), ("raw_obpm", "obpm")):
        weighted = sum(r[raw] * r["share"] for r in rows)
        # BPM sums to the team's net margin; OBPM to its offence against the
        # league's, since an average offence should sum to zero rather than to
        # half of whatever the net margin happens to be
        target = team_rtg if raw == "raw_bpm" else (team_ortg - lg_ortg)
        adj = (target - weighted) / tot_share
        for r in rows:
            r[final] = r[raw] + adj

    games = team.get("gp") or 82
    for r in rows:
        p = r["p"]
        p["pos_est"] = round(r["pos"], 2)
        p["role_est"] = round(r["role"], 2)
        p["bpm"] = round(r["bpm"], 2)
        p["obpm"] = round(r["obpm"], 2)
        p["dbpm"] = round(r["bpm"] - r["obpm"], 2)
        p["vorp"] = round((r["bpm"] + 2.0) * r["share"] * (games / 82.0), 2)


def run(env, only=None, dry=False, baseline=PTS_BASELINE):
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    con = duckdb.connect()

    seasons, token = set(), None
    while True:
        kw = {"Bucket": bucket, "Prefix": "v2/seasons/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) >= 3:
                seasons.add(parts[2])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    todo = sorted([s for s in seasons if not only or s == only], reverse=True)

    for season in todo:
        with tempfile.TemporaryDirectory() as tmp:
            ps = os.path.join(tmp, "ps.parquet")
            ts = os.path.join(tmp, "ts.parquet")
            try:
                s3.download_file(bucket, "%s/seasons/%s/player_season.parquet" % (PREFIX, season), ps)
                s3.download_file(bucket, "%s/seasons/%s/team_season.parquet" % (PREFIX, season), ts)
            except Exception as e:
                print("  %-9s missing export (%s)" % (season, str(e)[:40]))
                continue

            prows = [dict(zip([d[0] for d in con.execute("SELECT * FROM read_parquet('%s') LIMIT 0" % ps).description], r))
                     for r in con.execute("SELECT * FROM read_parquet('%s')" % ps).fetchall()]
            trows = [dict(zip([d[0] for d in con.execute("SELECT * FROM read_parquet('%s') LIMIT 0" % ts).description], r))
                     for r in con.execute("SELECT * FROM read_parquet('%s')" % ts).fetchall()]

            teams = {(t["team_abbr"], t["season_type"]): t for t in trows}

            # league offensive rating per season type, the baseline OBPM is
            # measured against
            lg = {}
            for t in trows:
                st = t["season_type"]
                a = lg.setdefault(st, [0.0, 0.0])
                a[0] += _f(t["pts"])
                a[1] += _f(t["poss_est"])
            lg_ortg = {st: (100.0 * v[0] / v[1] if v[1] else 110.0) for st, v in lg.items()}
            groups = {}
            for p in prows:
                groups.setdefault((p["last_team"], p["season_type"]), []).append(p)

            done = 0
            for key, members in groups.items():
                t = teams.get(key)
                if t:
                    bpm_for_team(members, t, lg_ortg.get(key[1], 110.0), baseline)
                    done += 1

            have = [p for p in prows if p.get("bpm") is not None]
            if dry:
                # BPM is a rate, so a short sample produces nonsense at the top
                # end: eight minutes and eight points reads as 49 points per 100.
                # Basketball Reference computes it for everyone too and filters
                # every leaderboard it publishes; 1000 minutes is their cut.
                qualified = [p for p in have
                             if (p.get("mp") or 0) >= 1000 and p["season_type"] == "regular"]
                print("  %-9s %d teams, %d players with BPM, %d at 1000+ min"
                      % (season, done, len(have), len(qualified)))
                top = sorted(qualified, key=lambda x: -x["bpm"])[:8]
                for p in top:
                    print("     %-24s %4.0f min  pos %.1f  BPM %+5.1f  OBPM %+5.1f  "
                          "DBPM %+5.1f  VORP %4.1f"
                          % (p["full_name"][:24], p["mp"], p["pos_est"],
                             p["bpm"], p["obpm"], p["dbpm"], p["vorp"]))
                continue

            # rewrite player_season with the new columns
            import json
            aug = os.path.join(tmp, "aug.json")
            with open(aug, "w", encoding="utf-8") as f:
                for p in prows:
                    f.write(json.dumps({
                        "player_id": p["player_id"], "season_type": p["season_type"],
                        "bpm": p.get("bpm"), "obpm": p.get("obpm"),
                        "dbpm": p.get("dbpm"), "vorp": p.get("vorp"),
                        "pos_est": p.get("pos_est"), "role_est": p.get("role_est"),
                    }) + "\n")
            out = os.path.join(tmp, "out.parquet")
            con.execute("""
                COPY (SELECT p.*, a.bpm, a.obpm, a.dbpm, a.vorp, a.pos_est, a.role_est
                      FROM read_parquet('%s') p
                      LEFT JOIN read_json_auto('%s') a
                        ON a.player_id = p.player_id AND a.season_type = p.season_type)
                TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (ps, aug, out))
            s3.upload_file(out, bucket, "%s/seasons/%s/player_season.parquet" % (PREFIX, season))
            print("  %-9s %d teams, %d players, %.0f KB"
                  % (season, done, len(have), os.path.getsize(out) / 1024))

    con.close()
    if dry:
        print("\n  dry run — nothing written")
    else:
        print("\n  done. Compare a few players against Basketball Reference;")
        print("  if they sit consistently high or low, adjust --baseline.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--baseline", type=float, default=PTS_BASELINE)
    a = ap.parse_args()
    run(load_env(), a.season, a.dry_run, a.baseline)
