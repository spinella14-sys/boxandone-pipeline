#!/usr/bin/env python3
"""
build_onoff.py — how a team plays with a player on the floor and with him off.

Every lineup stint (build_lineups.py) carries both teams' box score for the
stretch: makes and attempts, threes, free throws, offensive and defensive
rebounds, turnovers, assists, steals, blocks, points and possessions. Summed
over the stints a player was on the floor, and over the rest of his team's
minutes, they give the team's and the opponent's full line with him on and
with him off. Every team rate (OREB%, eFG%, TOV%, opponent eFG% ...) can then
be read on, off and against the league, in the page, from these totals.

Only games whose substitutions reconcile are used. Regular season, play-in
and playoffs are kept apart.

    python3 build_onoff.py build                         # every lineup season
    python3 build_onoff.py build --seasons 2026-27       # the nightly step
    python3 build_onoff.py show "Nikola Jokic" --season 2025-26

Writes v2/lineups/{season}_onoff.parquet: one row per player, team and season
type, with on_* and off_* totals, each split into t_ (his team) and o_
(the opponent). One row with player_id '_league' carries the league's totals.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)

LINEUPS = os.path.join(HOME, "data", "lineups")
FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
STATS = ("pts", "poss") + FACTORS
FIRST = "2019-20"


def all_seasons(last):
    out, y = [], int(FIRST[:4])
    while y <= int(last[:4]):
        out.append("%d-%s" % (y, str(y + 1)[2:]))
        y += 1
    return out


def stints_file(s3, bucket, season):
    """The season's stints: this machine's copy if it has one, else R2's."""
    p = os.path.join(LINEUPS, "%s.parquet" % season)
    if os.path.exists(p):
        return p
    os.makedirs(LINEUPS, exist_ok=True)
    try:
        s3.download_file(bucket, "v2/lineups/%s.parquet" % season, p)
        return p
    except Exception:
        return None


def build(con, season, path):
    """-> rows: per player, team and season type, on/off totals for both sides."""
    cols = {c for (c,) in con.execute("SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
                                      [path]).fetchall()}
    if "home_fgm" not in cols:
        print("  %s: stints carry no box-score tallies — rebuild lineups first" % season)
        return []

    def side(me, opp):
        mine = ", ".join("%s_%s AS t_%s" % (me, k, k) for k in FACTORS)
        theirs = ", ".join("%s_%s AS o_%s" % (opp, k, k) for k in FACTORS)
        return """SELECT game_id, %s AS team, %s_ids AS ids, seconds / 60.0 AS mins,
                         %s_pts AS t_pts, %s_pts AS o_pts, %s_poss AS t_poss, %s_poss AS o_poss, %s, %s
                  FROM read_parquet('%s') WHERE reliable""" % (me, me, me, opp, me, opp, mine, theirs, path)

    con.execute("CREATE OR REPLACE TEMP TABLE s AS SELECT *, CASE substr(game_id, 1, 3) "
                "WHEN '002' THEN 'regular' WHEN '004' THEN 'playoff' WHEN '005' THEN 'playin' END AS stype "
                "FROM (%s UNION ALL %s)" % (side("home", "away"), side("away", "home")))
    sums = ", ".join("SUM(%s_%s) AS %s_%s" % (w, k, w, k) for w in ("t", "o") for k in STATS)
    con.execute("""CREATE OR REPLACE TEMP TABLE team AS
                   SELECT stype, team, SUM(mins) AS mins, %s FROM s WHERE stype IS NOT NULL GROUP BY 1, 2""" % sums)
    con.execute("""CREATE OR REPLACE TEMP TABLE onc AS
                   SELECT stype, team, CAST(TRIM(x) AS BIGINT) AS nba_id, SUM(mins) AS mins, %s
                   FROM (SELECT *, UNNEST(string_split(ids, ',')) AS x FROM s WHERE stype IS NOT NULL)
                   WHERE TRIM(x) <> '' GROUP BY 1, 2, 3""" % sums)
    on_cols = ", ".join("o.%s_%s AS on_%s_%s" % (w, k, w, k) for w in ("t", "o") for k in STATS)
    off_cols = ", ".join("tm.%s_%s - o.%s_%s AS off_%s_%s" % (w, k, w, k, w, k) for w in ("t", "o") for k in STATS)
    rows = con.execute("""
        SELECT i.player_id, ? AS season, o.stype AS season_type, o.team AS team_abbr,
               ROUND(o.mins, 1) AS on_min, ROUND(tm.mins - o.mins, 1) AS off_min, %s, %s
        FROM onc o JOIN team tm USING (stype, team)
        LEFT JOIN ids i ON i.nba_id = o.nba_id
        WHERE i.player_id IS NOT NULL""" % (on_cols, off_cols), [season]).fetchall()
    names = [d[0] for d in con.description]
    out = [dict(zip(names, r)) for r in rows]
    # the league, for "against the league": every team's minutes, once
    lg = con.execute("SELECT stype, SUM(mins), %s FROM team GROUP BY 1" %
                     ", ".join("SUM(%s_%s)" % (w, k) for w in ("t", "o") for k in STATS)).fetchall()
    for r in lg:
        row = {"player_id": "_league", "season": season, "season_type": r[0], "team_abbr": "LG",
               "on_min": round(r[1], 1), "off_min": 0.0}
        for i, (w, k) in enumerate((w, k) for w in ("t", "o") for k in STATS):
            row["on_%s_%s" % (w, k)] = r[2 + i]
            row["off_%s_%s" % (w, k)] = 0
        out.append(row)
    return out


def write(con, rows, path):
    import json
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in rows:
            f.write(json.dumps(r, default=float) + "\n")
        tmp = f.name
    try:
        con.execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                    "TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (tmp, path))
    finally:
        os.remove(tmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "show"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--seasons")
    ap.add_argument("--season")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()

    import duckdb
    import nightly2 as N
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    db = duckdb.connect(N.DB_PATH, read_only=True)
    ids = [(int(s), p) for s, p in db.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source = 'nba'").fetchall()
        if str(s).isdigit()]
    names = dict(db.execute("SELECT player_id, full_name FROM players").fetchall())
    db.close()
    con.execute("CREATE TEMP TABLE ids (nba_id BIGINT, player_id VARCHAR)")
    con.executemany("INSERT INTO ids VALUES (?, ?)", ids)

    if a.cmd == "show":
        from ingest_rosters import norm
        season = a.season or N.current_season()
        p = os.path.join(LINEUPS, "%s_onoff.parquet" % season)
        if not os.path.exists(p):
            sys.exit("  build %s first" % season)
        want = norm(a.name or "")
        pids = [k for k, v in names.items() if v and norm(v) == want]
        rate = lambda n, d: "%5.1f" % (100.0 * n / d) if d else "    -"
        for r in con.execute("SELECT * FROM read_parquet(?) WHERE player_id IN (SELECT UNNEST(?::VARCHAR[])) "
                             "OR player_id = '_league'", [p, pids]).fetchall():
            d = dict(zip([x[0] for x in con.description], r))
            print("\n  %s %s %s  on %.0f min, off %.0f min" % (names.get(d["player_id"], "League"),
                  d["team_abbr"], d["season_type"], d["on_min"], d["off_min"]))
            for w in ("on", "off"):
                g = lambda k: d["%s_%s" % (w, k)] or 0
                print("    %-3s  ORtg %s  DRtg %s  OREB%% %s  DREB%% %s  eFG%% %s  opp eFG%% %s  TOV%% %s  AST/FGM %s"
                      % (w, rate(g("t_pts"), g("t_poss")), rate(g("o_pts"), g("o_poss")),
                         rate(g("t_orb"), g("t_orb") + g("o_drb")), rate(g("t_drb"), g("t_drb") + g("o_orb")),
                         rate(g("t_fgm") + .5 * g("t_fg3m"), g("t_fga")), rate(g("o_fgm") + .5 * g("o_fg3m"), g("o_fga")),
                         rate(g("t_tov"), g("t_poss")), rate(g("t_ast"), g("t_fgm"))))
        return

    seasons = a.seasons.split(",") if a.seasons else all_seasons(N.current_season())
    for season in seasons:
        path = stints_file(s3, bucket, season)
        if not path:
            print("  %s: no stints on file" % season)
            continue
        rows = build(con, season, path)
        if not rows:
            continue
        out = os.path.join(LINEUPS, "%s_onoff.parquet" % season)
        if os.path.exists(out):
            os.remove(out)
        write(con, rows, out)
        types = {}
        for r in rows:
            if r["player_id"] != "_league":
                types[r["season_type"]] = types.get(r["season_type"], 0) + 1
        print("  %s: %s" % (season, ", ".join("%d %s player-teams" % (n, t) for t, n in sorted(types.items()))))
        if not a.no_upload:
            s3.upload_file(out, bucket, "v2/lineups/%s_onoff.parquet" % season)


if __name__ == "__main__":
    main()
