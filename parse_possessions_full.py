#!/usr/bin/env python3
"""
parse_possessions_full.py — possessions for all thirty seasons.

The earlier parser read DuckDB, which after archiving holds only the current
season. This reads the union of the archive cache and the hot database, the
same way export_full.py does, and writes one Parquet per season to R2 rather
than growing the hot file back toward 400 MB.

Coverage was checked before building: across 1996-97 to 2025-26 events per game
sit between 468 and 506, substitutions between 36 and 53, and free-throw
sequence labels never fall below 96.2%. The parse is as reliable in 1997 as it
is now, so there is no era boundary to work around.

    python3 parse_possessions_full.py check --season 1996-97
    python3 parse_possessions_full.py build            # all seasons
    python3 parse_possessions_full.py build --season 2015-16
    python3 parse_possessions_full.py status

Writes v2/possessions/{season}.parquet — about 3 MB a season.
"""

import argparse
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
CACHE = os.path.join(HOME, "data", "archive")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2/possessions"

sys.path.insert(0, HOME)
try:
    from parse_possessions import parse_game, clock_secs       # the proven parser
except ImportError:
    sys.exit("  parse_possessions.py must be in %s" % HOME)


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


def connect():
    """In-memory connection with the hot database attached read-only, so the
    archive Parquet and the live season can be queried as one."""
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS hot (READ_ONLY)" % DB_PATH)
    if not os.path.isdir(CACHE) or not os.listdir(CACHE):
        # GitHub's runner has no archive: the season under way is all in the
        # hot database, which is all the nightly asks for
        print("  no archive cache — hot database only")
        con.execute("CREATE VIEW ag AS SELECT * FROM hot.games")
        con.execute("CREATE VIEW ap AS SELECT * FROM hot.play_by_play")
        return con
    con.execute("""CREATE VIEW ag AS
        SELECT * FROM read_parquet('%s/*/games.parquet', union_by_name=true)
        UNION ALL BY NAME SELECT * FROM hot.games""" % CACHE)
    con.execute("""CREATE VIEW ap AS
        SELECT * FROM read_parquet('%s/*/play_by_play.parquet', union_by_name=true)
        UNION ALL BY NAME SELECT * FROM hot.play_by_play""" % CACHE)
    return con


def seasons_in(con):
    return [r[0] for r in con.execute(
        "SELECT DISTINCT season FROM ag ORDER BY season DESC").fetchall()]


def games_of(con, season):
    """Stream one season's events, grouped by game."""
    cur = con.execute("""
        SELECT e.game_id, e.action_id, e.period, e.clock_raw, e.action_type,
               e.sub_type, e.team_abbr, e.shot_result, e.shot_value, e.description,
               g.home_abbr, g.away_abbr, g.season_type
        FROM ap e JOIN ag g ON g.game_id = e.game_id
        WHERE g.season = ?
        ORDER BY e.game_id, e.action_id""", [season])
    cols = [d[0] for d in cur.description]
    game, rows, meta = None, [], None
    while True:
        batch = cur.fetchmany(100000)
        if not batch:
            break
        for r in batch:
            d = dict(zip(cols, r))
            d["secs"] = clock_secs(d.get("clock_raw"))
            if d["game_id"] != game:
                if rows:
                    yield game, rows, meta
                game, rows = d["game_id"], []
                meta = (d.get("home_abbr"), d.get("away_abbr"), d.get("season_type"))
            rows.append(d)
    if rows:
        yield game, rows, meta


COLS = ["game_id", "season", "period", "poss_num", "off_team", "def_team",
        "start_action", "end_action", "start_secs", "end_secs", "points", "end_reason",
        "season_type", "start_type", "orebs", "pts_first", "pts_after_oreb",
        "margin", "clutch", "garbage", "score_state", "opp_net", "opp_tier"]
COL_TYPES = ["VARCHAR", "VARCHAR", "SMALLINT", "INTEGER", "VARCHAR", "VARCHAR",
             "BIGINT", "BIGINT", "DOUBLE", "DOUBLE", "SMALLINT", "VARCHAR",
             "VARCHAR", "VARCHAR", "SMALLINT", "SMALLINT", "SMALLINT",
             "SMALLINT", "BOOLEAN", "BOOLEAN", "VARCHAR", "DOUBLE", "VARCHAR"]
STARTS = ["after_score", "def_rebound", "live_turnover", "dead_turnover", "period_start"]


def norm_type(t):
    s = "".join(c for c in str(t or "").lower() if c.isalpha())
    if s.startswith("playoff"):
        return "playoff"
    if s == "playin":
        return "playin"
    if s.startswith("regular") or not s:
        return "regular"
    return s


def garbage_time(period, left, margin):
    """4th quarter only; the lead it takes shrinks as the clock runs down."""
    if period != 4 or left is None:
        return False
    m = abs(margin)
    return (left <= 720 and m >= 25) or (left <= 540 and m >= 20) or (left <= 360 and m >= 10)


def score_state(margin):
    if margin >= 10:
        return "up_big"
    if margin > 0:
        return "up"
    if margin == 0:
        return "tied"
    return "down" if margin > -10 else "down_big"


def parse_season(con, season):
    """Every possession of the season, with its context (F4)."""
    rows = []
    games = 0
    for gid, events, meta in games_of(con, season):
        games += 1
        home, away, stype = meta or (None, None, None)
        stype = norm_type(stype)
        pts = {}
        for r in parse_game(events):
            off = r["off_team"]
            deff = away if off == home else home
            margin = pts.get(off, 0) - pts.get(deff, 0)
            left = r["start_secs"]
            per = r["period"] or 0
            rows.append({
                "game_id": gid, "season": season, "period": per, "poss_num": r["poss_num"],
                "off_team": off, "def_team": deff, "start_action": r["start_action"],
                "end_action": r["end_action"], "start_secs": left, "end_secs": r["end_secs"],
                "points": r["points"], "end_reason": r["end_reason"], "season_type": stype,
                "start_type": r.get("start_type"), "orebs": r.get("orebs", 0),
                "pts_first": r.get("pts_first", 0), "pts_after_oreb": r.get("pts_after_oreb", 0),
                "margin": margin,
                "clutch": bool(per >= 4 and left is not None and left <= 300 and abs(margin) <= 5),
                "garbage": garbage_time(per, left, margin),
                "score_state": score_state(margin), "opp_net": None, "opp_tier": None})
            pts[off] = pts.get(off, 0) + (r["points"] or 0)
    # opponent strength: each defence's regular-season net rating per 100
    tally = {}
    for r in rows:
        if r["season_type"] != "regular":
            continue
        for t, k in ((r["off_team"], "of"), (r["def_team"], "df")):
            d = tally.setdefault(t, {"of": 0, "on": 0, "df": 0, "dn": 0})
            d[k] += r["points"] or 0
            d["on" if k == "of" else "dn"] += 1
    net = {t: 100.0 * (d["of"] / d["on"] - d["df"] / d["dn"])
           for t, d in tally.items() if d["on"] and d["dn"]}
    order = sorted(net, key=lambda t: -net[t])
    tier = {t: ("top" if i < 10 else "bottom" if i >= len(order) - 10 else "middle")
            for i, t in enumerate(order)}
    for r in rows:
        r["opp_net"] = round(net[r["def_team"]], 2) if r["def_team"] in net else None
        r["opp_tier"] = tier.get(r["def_team"])
    return games, rows


def possession_values(rows, season):
    """F3: each season's point values, per season type."""
    out = []
    for stype in sorted({r["season_type"] for r in rows}):
        rs = [r for r in rows if r["season_type"] == stype]
        n = len(rs)
        if not n:
            continue
        ppp = sum(r["points"] for r in rs) / n
        v = {"season": season, "season_type": stype, "poss": n, "ppp": round(ppp, 4)}
        for st in STARTS:
            sub = [r for r in rs if r["start_type"] == st]
            v["poss_" + st] = len(sub)
            v["ppp_" + st] = round(sum(r["points"] for r in sub) / len(sub), 4) if sub else None
            v["chance_" + st] = round(sum(r["pts_first"] for r in sub) / len(sub), 4) if sub else None
        orebs = sum(r["orebs"] for r in rs)
        v["orebs"] = orebs
        v["chance_oreb"] = round(sum(r["pts_after_oreb"] for r in rs) / orebs, 4) if orebs else None
        # A turnover forfeits a possession worth ppp, and hands the opponent a
        # possession that begins after a turnover rather than the way it would
        # otherwise have begun: after a score or a defensive rebound. The cost
        # is the possession lost plus that difference.
        live, dead = v["ppp_live_turnover"], v["ppp_dead_turnover"]
        usual = [r for r in rs if r["start_type"] in ("after_score", "def_rebound")]
        base = sum(r["points"] for r in usual) / len(usual) if usual else ppp
        v["ppp_usual_start"] = round(base, 4)
        v["tov_value_live"] = round(ppp + (live - base), 4) if live is not None else None
        v["tov_value_dead"] = round(ppp + (dead - base), 4) if dead is not None else None
        nl, nd = v["poss_live_turnover"], v["poss_dead_turnover"]
        v["tov_value"] = (round((v["tov_value_live"] * nl + v["tov_value_dead"] * nd) / (nl + nd), 4)
                          if nl and nd else None)
        v["oreb_value"] = v["chance_oreb"]
        v["ts_point_per_tsa"] = 0.02
        cl = [r for r in rs if r["clutch"]]
        v["clutch_poss"] = len(cl)
        v["clutch_ppp"] = round(sum(r["points"] for r in cl) / len(cl), 4) if cl else None
        v["garbage_share"] = round(sum(1 for r in rs if r["garbage"]) / n, 4)
        out.append(v)
    return out


def write_rows(path, rows, cols, types):
    import duckdb
    w = duckdb.connect()
    w.execute("CREATE TABLE p (%s)" % ", ".join('"%s" %s' % (c, t) for c, t in zip(cols, types)))
    w.executemany("INSERT INTO p VALUES (%s)" % ",".join("?" * len(cols)),
                  [tuple(r[c] for c in cols) for r in rows])
    if os.path.exists(path):
        os.remove(path)
    w.execute("COPY p TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % path)
    w.close()


def write_values(s3, bucket, tmp, season, vals):
    """One file per season, then every season's together for the site."""
    import duckdb
    import json
    if vals:
        jl = os.path.join(tmp, "v.jsonl")
        with open(jl, "w") as f:
            for v in vals:
                f.write(json.dumps(v) + "\n")
        p = os.path.join(tmp, "v.parquet")
        if os.path.exists(p):
            os.remove(p)
        duckdb.connect().execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                                 "TO '%s' (FORMAT PARQUET)" % (jl, p))
        s3.upload_file(p, bucket, "v2/models/possession_values/%s.parquet" % season)


def cmd_check(season):
    con = connect()
    target = season or seasons_in(con)[0]
    games, rows = parse_season(con, target)
    pts = sum(r["points"] for r in rows)
    reasons = {}
    for r in rows:
        reasons[r["end_reason"]] = reasons.get(r["end_reason"], 0) + 1
    print("  %s" % target)
    print("    games        %d" % games)
    print("    possessions  %d  (%.1f per game, %.1f per team)"
          % (len(rows), len(rows) / games, len(rows) / games / 2))
    print("    points       %d  (%.3f per possession)" % (pts, pts / len(rows)))
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("    %-12s %7d  %5.1f%%" % (k, v, 100 * v / len(rows)))
    for v in possession_values(rows, target):
        print("\n    %s" % v["season_type"])
        for k in ("poss", "ppp", "ppp_usual_start", "ppp_after_score", "ppp_def_rebound", "ppp_live_turnover",
                  "ppp_dead_turnover", "ppp_period_start", "chance_oreb", "tov_value_live",
                  "tov_value_dead", "tov_value", "oreb_value", "clutch_poss", "clutch_ppp",
                  "garbage_share"):
            print("      %-18s %s" % (k, v.get(k)))
    con.close()


def cmd_build(env, season):
    import duckdb
    con = connect()
    s3 = s3c(env)
    targets = [season] if season else seasons_in(con)
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for s in targets:
            games, rows = parse_season(con, s)
            if not rows:
                print("  %-9s no events" % s)
                continue
            path = os.path.join(tmp, "p.parquet")
            write_rows(path, rows, COLS, COL_TYPES)
            s3.upload_file(path, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, s))
            vals = possession_values(rows, s)
            write_values(s3, env["R2_BUCKET_NAME"], tmp, s, vals)
            reg = [v for v in vals if v["season_type"] == "regular"]
            if reg:
                v = reg[0]
                print("      values: ppp %.3f · live TO %.2f · dead TO %.2f · OREB %.2f · clutch %d poss"
                      % (v["ppp"], v["tov_value_live"] or 0, v["tov_value_dead"] or 0,
                         v["oreb_value"] or 0, v["clutch_poss"]))
            pts = sum(r["points"] for r in rows)
            total += len(rows)
            print("  %-9s %5d games  %7d poss  %5.1f/g  %.3f ppp  %4.0f KB"
                  % (s, games, len(rows), len(rows) / games, pts / len(rows),
                     os.path.getsize(path) / 1024))
    con.close()
    combine_values(env)
    print("\n  %d possessions across %d seasons" % (total, len(targets)))


def combine_values(env):
    """Every season's values in one small file for the site."""
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix="v2/models/possession_values/"):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    if not keys:
        return
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, k in enumerate(keys):
            p = os.path.join(tmp, "%d.parquet" % i)
            s3.download_file(bucket, k, p)
            paths.append(p)
        out = os.path.join(tmp, "all.parquet")
        duckdb.connect().execute(
            "COPY (SELECT * FROM read_parquet(%s, union_by_name=true) ORDER BY season, season_type) "
            "TO '%s' (FORMAT PARQUET)" % (repr(paths), out))
        s3.upload_file(out, bucket, "v2/models/possession_values.parquet")
    print("  possession values: %d seasons combined" % len(keys))


def cmd_status(env):
    import duckdb
    s3 = s3c(env)
    bucket = env["R2_BUCKET_NAME"]
    keys, token, size = [], None, 0
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            keys.append(o["Key"])
            size += o["Size"]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if not keys:
        print("  nothing built yet")
        return
    print("  %d seasons, %.1f MB" % (len(keys), size / 1048576.0))
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for k in sorted(keys, reverse=True)[:5]:
            p = os.path.join(tmp, "x.parquet")
            s3.download_file(bucket, k, p)
            r = con.execute("""SELECT COUNT(*), COUNT(DISTINCT game_id),
                SUM(points)::DOUBLE/COUNT(*) FROM read_parquet('%s')""" % p).fetchone()
            print("    %-28s %7d poss  %5d games  %.3f ppp"
                  % (k.split("/")[-1], r[0], r[1], r[2]))
            os.remove(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build", "status"])
    ap.add_argument("--season")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check(a.season)
    elif a.cmd == "build":
        cmd_build(load_env(), a.season)
    else:
        cmd_status(load_env())
