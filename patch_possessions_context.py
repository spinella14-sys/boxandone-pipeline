#!/usr/bin/env python3
"""
patch_possessions_context.py — run once. Foundations F3 and F4.

F3, possession accounting: what a turnover, an offensive rebound and a point of
TS% are worth in points, each season. F4, game context: clutch, garbage time,
playoff, opponent strength, score state, tagged on every possession.

parse_possessions.py (the proven parser) now also records, per possession:
  start_type      how it began: after_score, def_rebound, live_turnover,
                  dead_turnover, period_start. A live turnover is a bad pass or
                  lost ball (the same rule as the Stat Database's Live TO%).
  orebs           offensive rebounds inside it (each one a new chance)
  pts_first       points on the first chance, before any offensive rebound
  pts_after_oreb  points after the first offensive rebound

parse_possessions_full.py adds the context to every row and writes the values:
  season_type     regular / playoff / play-in …
  margin          the offence's lead when the possession began (negative = behind)
  clutch          4th quarter or overtime, 5:00 or less left, within 5 points
                  (the NBA's definition, as decided)
  garbage         4th quarter, margin at least 25 with 12:00 or less, 20 with
                  9:00, 10 with 6:00 (a proposed rule: no overtime is garbage)
  score_state     up_big (10+), up, tied, down, down_big
  opp_net         the defence's net rating that regular season, per 100
  opp_tier        top (its best ten), middle, bottom (its worst ten)

  v2/models/possession_values/{season}.parquet and possession_values.parquet
  (every season together), one row per season and season type:
    poss, ppp                         points per possession
    ppp_{start}, chance_{start}       per possession that began that way, and
                                      its first chance only
    chance_oreb                       points per chance an offensive rebound bought
    tov_value_live / _dead / tov_value  what a turnover costs: the possession
                                      forfeited (ppp) plus what the opponent's
                                      next possession gains over one that began
                                      after a score or a defensive rebound
    oreb_value                        what an offensive rebound buys: the extra
                                      chance (chance_oreb)
    ts_point_per_tsa = 0.02           one TS% point is 0.02 points per true shot
                                      attempt, by definition, in any season
    clutch_ppp, garbage_share

Then rebuild the seasons (on the laptop, which has the archive):
    python3 parse_possessions_full.py build
The nightly keeps the current season up to date as before.
"""
import os
import sys

HOME = os.path.expanduser("~/boxandone")


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %-26s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s not as expected near:\n%s\n  nothing written to it" % (name, old[:120]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %-26s patched" % name)


# ---------------------------------------------------------------- the parser
patch("parse_possessions.py", [
    ('''    cur = None          # the possession being built
    poss_num = 0
    last_shot_team = None   # whose miss is on the glass right now
''',
     '''    cur = None          # the possession being built
    poss_num = 0
    last_shot_team = None   # whose miss is on the glass right now
    next_start = "period_start"   # how the next possession begins
    last_to_live = False

    def score(n):
        # points on the first chance, or after an offensive rebound bought another
        cur["points"] += n
        if cur["orebs"]:
            cur["pts_after_oreb"] += n
        else:
            cur["pts_first"] += n
'''),
    ('''    def close(reason, action, secs):
        nonlocal cur
        if cur and cur["off_team"]:
            cur["end_action"] = action
            cur["end_secs"] = secs
            cur["end_reason"] = reason
            out.append(cur)
        cur = None
''',
     '''    def close(reason, action, secs):
        nonlocal cur, next_start
        if cur and cur["off_team"]:
            cur["end_action"] = action
            cur["end_secs"] = secs
            cur["end_reason"] = reason
            out.append(cur)
        cur = None
        next_start = {"made_fg": "after_score", "made_ft": "after_score",
                      "def_rebound": "def_rebound",
                      "turnover": "live_turnover" if last_to_live else "dead_turnover",
                      }.get(reason, "period_start")
'''),
    ('''        cur = {"period": period, "poss_num": poss_num, "off_team": team,
               "def_team": None, "start_action": action, "start_secs": secs,
               "points": 0}''',
     '''        cur = {"period": period, "poss_num": poss_num, "off_team": team,
               "def_team": None, "start_action": action, "start_secs": secs,
               "points": 0, "start_type": next_start, "orebs": 0,
               "pts_first": 0, "pts_after_oreb": 0}'''),
    ('''        if e.get("period") != period:
            close("period_end", action, secs)
            period = e.get("period")
            poss_num = 0
            last_shot_team = None''',
     '''        if e.get("period") != period:
            close("period_end", action, secs)
            period = e.get("period")
            poss_num = 0
            last_shot_team = None
            next_start = "period_start"'''),
    ('''            if cur and cur["off_team"] == team:
                cur["points"] += (e.get("shot_value") or 2)''',
     '''            if cur and cur["off_team"] == team:
                score(e.get("shot_value") or 2)'''),
    ('''                if team == last_shot_team:
                    pass                      # offensive — possession continues''',
     '''                if team == last_shot_team:
                    # offensive — the possession continues with a new chance
                    if cur and cur["off_team"] == team:
                        cur["orebs"] += 1'''),
    ('''        elif at == "turnover":
            if cur and cur["off_team"] == team:''',
     '''        elif at == "turnover":
            last_to_live = st in ("Bad Pass", "Lost Ball")
            if cur and cur["off_team"] == team:'''),
    ('''            if cur and cur["off_team"] == team and made:
                cur["points"] += 1''',
     '''            if cur and cur["off_team"] == team and made:
                score(1)'''),
], "next_start")


# ---------------------------------------------------------- the season build
patch("parse_possessions_full.py", [
    ('''               g.home_abbr, g.away_abbr
        FROM ap e JOIN ag g ON g.game_id = e.game_id''',
     '''               g.home_abbr, g.away_abbr, g.season_type
        FROM ap e JOIN ag g ON g.game_id = e.game_id'''),
    ('''                meta = (d.get("home_abbr"), d.get("away_abbr"))''',
     '''                meta = (d.get("home_abbr"), d.get("away_abbr"), d.get("season_type"))'''),
    ('''def parse_season(con, season):
    out = []
    games = 0
    for gid, events, meta in games_of(con, season):
        games += 1
        home, away = meta or (None, None)
        for r in parse_game(events):
            off = r["off_team"]
            deff = away if off == home else home
            out.append((gid, season, r["period"], r["poss_num"], off, deff,
                        r["start_action"], r["end_action"],
                        r["start_secs"], r["end_secs"], r["points"], r["end_reason"]))
    return games, out
''',
     '''COLS = ["game_id", "season", "period", "poss_num", "off_team", "def_team",
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
                f.write(json.dumps(v) + "\\n")
        p = os.path.join(tmp, "v.parquet")
        if os.path.exists(p):
            os.remove(p)
        duckdb.connect().execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                                 "TO '%s' (FORMAT PARQUET)" % (jl, p))
        s3.upload_file(p, bucket, "v2/models/possession_values/%s.parquet" % season)
'''),
    ('''            games, rows = parse_season(con, s)
            if not rows:
                print("  %-9s no events" % s)
                continue
            w = duckdb.connect()
            w.execute("""CREATE TABLE p (game_id VARCHAR, season VARCHAR,
                period SMALLINT, poss_num INTEGER, off_team VARCHAR,
                def_team VARCHAR, start_action BIGINT, end_action BIGINT,
                start_secs DOUBLE, end_secs DOUBLE, points SMALLINT,
                end_reason VARCHAR)""")
            w.executemany("INSERT INTO p VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            path = os.path.join(tmp, "p.parquet")
            if os.path.exists(path):
                os.remove(path)
            w.execute("COPY p TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % path)
            w.close()
            s3.upload_file(path, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, s))
            pts = sum(r[10] for r in rows)''',
     '''            games, rows = parse_season(con, s)
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
            pts = sum(r["points"] for r in rows)'''),
    ('''    con.close()
    print("\\n  %d possessions across %d seasons" % (total, len(targets)))''',
     '''    con.close()
    combine_values(env)
    print("\\n  %d possessions across %d seasons" % (total, len(targets)))


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
    print("  possession values: %d seasons combined" % len(keys))'''),
    ('''    pts = sum(r[10] for r in rows)
    reasons = {}
    for r in rows:
        reasons[r[11]] = reasons.get(r[11], 0) + 1''',
     '''    pts = sum(r["points"] for r in rows)
    reasons = {}
    for r in rows:
        reasons[r["end_reason"]] = reasons.get(r["end_reason"], 0) + 1'''),
    ('''    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("    %-12s %7d  %5.1f%%" % (k, v, 100 * v / len(rows)))
    con.close()''',
     '''    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("    %-12s %7d  %5.1f%%" % (k, v, 100 * v / len(rows)))
    for v in possession_values(rows, target):
        print("\\n    %s" % v["season_type"])
        for k in ("poss", "ppp", "ppp_usual_start", "ppp_after_score", "ppp_def_rebound", "ppp_live_turnover",
                  "ppp_dead_turnover", "ppp_period_start", "chance_oreb", "tov_value_live",
                  "tov_value_dead", "tov_value", "oreb_value", "clutch_poss", "clutch_ppp",
                  "garbage_share"):
            print("      %-18s %s" % (k, v.get(k)))
    con.close()'''),
], "possession_values")
print("\n  then: python3 parse_possessions_full.py check --season 2025-26")
