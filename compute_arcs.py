#!/usr/bin/env python3
"""
compute_arcs.py — career-arc points: a rolling 200-minute window, every game.

For each game a player appears in (preseason excluded), the window is that
game plus as many earlier games as it takes to reach 200 minutes. The window
can run back across an offseason, so a player's mark at the start of a season
still rests on 200 minutes. The point is plotted at his exact age that night.

What is measured over each window:

  bpm / obpm / dbpm   BPM 2.0 on the window's box totals. Position, offensive
                      role and the team adjustment are the player's values for
                      that regular season (compute_bpm.py), so a 200-minute
                      sample is never asked to estimate them itself. Pace is
                      each game's own team's, game by game.
  ts_rel              true shooting over the window minus that season's league.

This is the stand-in line until JARED exists. BPM is approximate (see
compute_bpm.py) and a 200-minute window is noisy: the noise is measured here,
from back-to-back windows in the same season, and written to the meta file so
the chart can draw it as the line's range.

    python3 compute_arcs.py check --name "Nikola Jokic"   # inspect, write nothing
    python3 compute_arcs.py build --all                    # every player (Mac)
    python3 compute_arcs.py build --season 2026-27         # players in a season
    python3 compute_arcs.py build --players P00000123,P00000456

Writes v2/players/{player_id}/arc.parquet and v2/config/arc_meta.json. --all reads
the local archive; --season and --players read each player's career game log
from R2, so the nightly job can run it without the archive.
"""
import argparse
import json
import math
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PREFIX = "v2"
WINDOW = 200.0          # minutes
QUAL_MP = 500           # a season counts toward the league distribution
sys.path.insert(0, HOME)

import compute_bpm as B  # noqa: E402

STATS = ("pts", "fg3m", "ast", "tov", "orb", "drb", "stl", "blk", "pf", "fga", "fta")


# ------------------------------------------------------------------ context

def season_context(s3, bucket, tmp, con):
    """Download every season's player and team files; return per-season
    parameters for each player, team pace, and the league distribution."""
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX + "/seasons/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])
                 if o["Key"].endswith(("/player_season.parquet", "/team_season.parquet"))]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    def fetch(k):
        p = os.path.join(tmp, k.replace("/", "_"))
        s3.download_file(bucket, k, p)
        return k, p

    with ThreadPoolExecutor(max_workers=16) as pool:
        files = dict(pool.map(fetch, keys))

    def rows(path):
        cur = con.execute("SELECT * FROM read_parquet('%s')" % path)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    pace, team_ctx, lg_ts, params, dist, sbpm = {}, {}, {}, {}, {}, {}
    for k, path in files.items():
        if not k.endswith("team_season.parquet"):
            continue
        season = k.split("/")[2]
        pts = tsa = 0.0
        for t in rows(path):
            tm_mp, poss = B._f(t.get("team_mp")), B._f(t.get("poss_est"))
            if tm_mp and poss:
                pace[(t["team_abbr"], season, t["season_type"])] = poss * 5.0 / tm_mp
            if t["season_type"] == "regular":
                t_tsa = B._f(t.get("fga")) + 0.44 * B._f(t.get("fta"))
                team_ctx[(t["team_abbr"], season)] = (
                    (B._f(t.get("pts")) / t_tsa if t_tsa else B.PTS_BASELINE) - B.PTS_BASELINE)
                pts += B._f(t.get("pts"))
                tsa += t_tsa
        lg_ts[season] = pts / (2 * tsa) if tsa else 0.55

    for k, path in files.items():
        if not k.endswith("player_season.parquet"):
            continue
        season = k.split("/")[2]
        qual = []
        for p in rows(path):
            if p.get("season_type") != "regular" or p.get("bpm") is None:
                continue
            ppm = pace.get((p.get("last_team"), season, "regular"))
            ctx = team_ctx.get((p.get("last_team"), season))
            mp = B._f(p.get("mp"))
            if not ppm or ctx is None or mp <= 0:
                continue
            pos, role = B._f(p.get("pos_est")), B._f(p.get("role_est"))
            tot = {s: B._f(p.get(s)) for s in STATS}
            raw_b, raw_o = raw_bpm(tot, mp * ppm, pos, role, ctx)
            params[(p["player_id"], season)] = (
                pos, role, ctx, B._f(p["bpm"]) - raw_b, B._f(p.get("obpm")) - raw_o)
            sbpm[(p["player_id"], season)] = (round(B._f(p["bpm"]), 1), round(mp))
            if mp >= QUAL_MP:
                qual.append(round(B._f(p["bpm"]), 2))
        dist[season] = sorted(qual, reverse=True)
    return pace, lg_ts, params, dist, sbpm


def raw_bpm(tot, player_poss, pos, role, ctx):
    """BPM 2.0 before the team adjustment, exactly as compute_bpm applies it."""
    if player_poss <= 0:
        return None, None
    per100 = {k: 100.0 * tot[k] / player_poss for k in STATS}
    tsa100 = 100.0 * (tot["fga"] + 0.44 * tot["fta"]) / player_poss
    out = []
    for coeffs, role_c, pos_k, role_k in ((B.BPM_C, B.BPM_ROLE, B.BPM_POS_CONST, B.BPM_ROLE_CONST),
                                          (B.OBPM_C, B.OBPM_ROLE, B.OBPM_POS_CONST, B.OBPM_ROLE_CONST)):
        v = 0.0
        for k, pair in coeffs.items():
            stat = per100[k] - (ctx * tsa100 if k == "pts" else 0.0)
            v += B.lerp(pair, pos) * stat
        for k, pair in role_c.items():
            v += B.lerp(pair, role) * per100[k]
        v += (3.0 - pos) * (pos_k / 2.0) if pos < 3.0 else 0.0
        v += (3.0 - role) * (role_k / 2.0)
        out.append(v)
    return out[0], out[1]


# ------------------------------------------------------------------- games

GAMES_FROM_DB = """
SELECT b.player_id, b.game_id, g.game_date, g.season, g.season_type, b.team_abbr,
       b.seconds_played/60.0 AS mp, %s,
       date_diff('day', p.birthdate, g.game_date)/365.25 AS age
FROM all_box b JOIN all_games g ON g.game_id = b.game_id
JOIN hot.players p ON p.player_id = b.player_id
WHERE b.played AND b.seconds_played > 0 AND g.season_type <> 'preseason' %s
""" % (", ".join("b.%s" % s for s in STATS), "%s")

GAMES_FROM_LOGS = """
SELECT regexp_extract(filename, 'players_(P[0-9]+)_gamelog', 1) AS player_id,
       game_id, game_date, season, season_type, team_abbr, mp, %s, age
FROM read_parquet(%s, filename=true, union_by_name=true)
WHERE mp > 0 AND season_type <> 'preseason'
""" % (", ".join(STATS), "%s")


def windows(con):
    """Every game's trailing 200-minute window, as running-total differences.
    An ASOF join finds, for each game, the last earlier game whose running
    minutes sit at least 200 below this one's."""
    cum = ",\n".join("SUM(%s) OVER w AS c_%s" % (s, s) for s in STATS)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE c AS
        SELECT player_id, game_id, game_date, season, season_type, team_abbr, age,
               ROW_NUMBER() OVER w AS rn,
               SUM(mp) OVER w AS c_mp,
               SUM(mp) OVER w - %f AS back,
               SUM(mp * ppm) OVER w AS c_poss,
               %s
        FROM gl
        WINDOW w AS (PARTITION BY player_id ORDER BY game_date, game_id
                     ROWS UNBOUNDED PRECEDING)""" % (WINDOW, cum))
    diffs = ",\n".join("i.c_%s - COALESCE(k.c_%s, 0) AS %s" % (s, s, s) for s in STATS)
    cur = con.execute("""
        SELECT i.player_id, i.game_id, i.game_date, i.season, i.season_type,
               i.team_abbr, i.age, i.rn, k.rn AS k_rn,
               i.c_mp - COALESCE(k.c_mp, 0) AS mp,
               i.rn - COALESCE(k.rn, 0) AS games,
               i.c_poss - COALESCE(k.c_poss, 0) AS poss,
               %s
        FROM c i ASOF LEFT JOIN c k
          ON i.player_id = k.player_id AND i.back >= k.c_mp
        WHERE i.c_mp >= %f
        ORDER BY i.player_id, i.rn""" % (diffs, WINDOW))
    cols = [d[0] for d in cur.description]
    while True:
        batch = cur.fetchmany(50000)
        if not batch:
            break
        for r in batch:
            yield dict(zip(cols, r))


def compute(con, params, lg_ts):
    out, by_rn, noise = [], {}, []
    for w in windows(con):
        prm = params.get((w["player_id"], w["season"]))
        if not prm or not w["poss"]:
            continue
        pos, role, ctx, adj_b, adj_o = prm
        tot = {s: B._f(w[s]) for s in STATS}
        rb, ro = raw_bpm(tot, B._f(w["poss"]), pos, role, ctx)
        if rb is None:
            continue
        bpm, obpm = rb + adj_b, ro + adj_o
        tsa = tot["fga"] + 0.44 * tot["fta"]
        ts_rel = (tot["pts"] / (2 * tsa) - lg_ts.get(w["season"], 0.55)) if tsa else None
        row = (w["player_id"], w["game_id"], w["game_date"], w["season"], w["season_type"],
               w["team_abbr"], round(w["age"], 3) if w["age"] is not None else None,
               round(B._f(w["mp"]), 1), int(w["games"]),
               round(bpm, 2), round(obpm, 2), round(bpm - obpm, 2),
               round(ts_rel, 4) if ts_rel is not None else None)
        out.append(row)
        # the window that ended exactly where this one starts: same season,
        # regular season both, no overlap — their gap is the noise estimate
        key = (w["player_id"], w["rn"])
        by_rn[key] = (bpm, w["season"], w["season_type"])
        prev = by_rn.get((w["player_id"], w["k_rn"])) if w["k_rn"] else None
        if prev and prev[1] == w["season"] and prev[2] == w["season_type"] == "regular":
            noise.append(bpm - prev[0])
    sd = (math.sqrt(sum(d * d for d in noise) / len(noise)) / math.sqrt(2)) if noise else None
    return out, sd, len(noise)


COLS = ("player_id VARCHAR, game_id VARCHAR, game_date DATE, season VARCHAR, "
        "season_type VARCHAR, team_abbr VARCHAR, age DOUBLE, win_mp DOUBLE, "
        "win_games INTEGER, bpm DOUBLE, obpm DOUBLE, dbpm DOUBLE, ts_rel DOUBLE")


# -------------------------------------------------------------------- main

def env_and_s3():
    import nightly2 as N
    env = N.load_env()
    return env, N.s3c(env)


def load_games(con, a, s3, bucket, tmp):
    if a.all or a.cmd == "check":
        import export_full as X
        db = X.connect(X.load_env())
        con.close()
        con = db
        where = ""
        if a.cmd == "check":
            name = a.name.replace("'", "''")
            where = ("AND b.player_id IN (SELECT player_id FROM hot.players "
                     "WHERE strip_accents(lower(full_name)) LIKE strip_accents(lower('%%%s%%')))" % name)
        con.execute("CREATE TEMP TABLE g0 AS " + GAMES_FROM_DB % where)
        return con
    if a.season:
        import duckdb
        h = duckdb.connect(DB_PATH, read_only=True)
        pids = [r[0] for r in h.execute("""
            SELECT DISTINCT b.player_id FROM player_game_box b
            JOIN games g ON g.game_id = b.game_id
            WHERE g.season = ? AND b.played""", [a.season]).fetchall()]
        h.close()
    else:
        pids = [p.strip() for p in a.players.split(",") if p.strip()]

    def fetch(pid):
        p = os.path.join(tmp, "players_%s_gamelog.parquet" % pid)
        try:
            s3.download_file(bucket, "%s/players/%s/gamelog.parquet" % (PREFIX, pid), p)
            return p
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        paths = [p for p in pool.map(fetch, pids) if p]
    print("  %d players, %d career game logs fetched" % (len(pids), len(paths)))
    if not paths:
        sys.exit("  nothing to compute")
    con.execute("CREATE TEMP TABLE g0 AS " + GAMES_FROM_LOGS % ("[%s]" % ",".join(
        "'%s'" % p for p in paths)))
    return con


def main():
    import duckdb
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--season")
    ap.add_argument("--players")
    ap.add_argument("--name", default="Nikola Jok")
    a = ap.parse_args()
    if a.cmd == "build" and not (a.all or a.season or a.players):
        sys.exit("  give --all, --season or --players")

    env, s3 = env_and_s3()
    bucket = env["R2_BUCKET_NAME"]
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect()
        pace, lg_ts, params, dist, sbpm = season_context(s3, bucket, tmp, con)
        print("  season context: %d seasons, %d player-seasons with BPM"
              % (len(dist), len(params)))
        con = load_games(con, a, s3, bucket, tmp)

        # each game's own team pace; a season type without a team row (the
        # Cup final, say) borrows that team's regular season
        con.execute("CREATE TEMP TABLE pace (team_abbr VARCHAR, season VARCHAR, "
                    "season_type VARCHAR, ppm DOUBLE)")
        con.executemany("INSERT INTO pace VALUES (?,?,?,?)",
                        [k + (v,) for k, v in pace.items()])
        con.execute("""
            CREATE TEMP TABLE gl AS
            SELECT g0.*, COALESCE(p1.ppm, p2.ppm) AS ppm FROM g0
            LEFT JOIN pace p1 ON p1.team_abbr = g0.team_abbr AND p1.season = g0.season
                             AND p1.season_type = g0.season_type
            LEFT JOIN pace p2 ON p2.team_abbr = g0.team_abbr AND p2.season = g0.season
                             AND p2.season_type = 'regular'
            WHERE COALESCE(p1.ppm, p2.ppm) IS NOT NULL""")
        n_games = con.execute("SELECT COUNT(*), COUNT(DISTINCT player_id) FROM gl").fetchone()
        print("  %d player-games across %d players" % n_games)

        rows, sd, n_pairs = compute(con, params, lg_ts)
        print("  %d arc points | window noise: BPM sd %.2f from %d back-to-back pairs"
              % (len(rows), sd or 0, n_pairs))

        con.execute("CREATE TEMP TABLE arc (%s)" % COLS)
        con.executemany("INSERT INTO arc VALUES (%s)" % ",".join("?" * 13), rows)

        if a.cmd == "check":
            for r in con.execute("""
                SELECT a.player_id, p.full_name, a.season, COUNT(*) pts,
                       ROUND(MIN(a.bpm),1), ROUND(MAX(a.bpm),1),
                       ROUND(SUM(a.bpm) / COUNT(*), 1) AS mean_window
                FROM arc a JOIN hot.players p ON p.player_id = a.player_id
                WHERE a.season_type = 'regular'
                GROUP BY 1,2,3 ORDER BY 2,3""").fetchall():
                sb, smp = sbpm.get((r[0], r[2]), (None, None))
                print("  %-22s %s  %3d points  window BPM %5.1f to %5.1f  mean %5.1f"
                      "   season BPM %s (%s min)"
                      % (r[1][:22], r[2], r[3], r[4], r[5], r[6], sb, smp))
            print("\n  (compare 'mean' with the season BPM on the Stats tab — they"
                  " should sit close, not equal: windows overlap and span offseasons)")
            print("  dry run — nothing written")
            return

        out = os.path.join(tmp, "arcs")
        con.execute("""COPY (SELECT * FROM arc ORDER BY player_id, game_date, game_id)
                       TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (player_id))""" % out)
        jobs = []
        for d in os.listdir(out) if os.path.isdir(out) else []:
            if d.startswith("player_id="):
                f = [x for x in os.listdir(os.path.join(out, d)) if x.endswith(".parquet")]
                if f:
                    jobs.append((d.split("=", 1)[1], os.path.join(out, d, f[0])))

        def up(job):
            pid, path = job
            s3.upload_file(path, bucket, "%s/players/%s/arc.parquet" % (PREFIX, pid),
                           ExtraArgs={"ContentType": "application/octet-stream"})
            return os.path.getsize(path)

        with ThreadPoolExecutor(max_workers=16) as pool:
            total = sum(pool.map(up, jobs))
        print("  %d arc files written, %.1f MB" % (len(jobs), total / 1048576.0))

        meta_key = "%s/config/arc_meta.json" % PREFIX   # config/ is already served by the data proxy
        meta = {}
        try:
            meta = json.loads(s3.get_object(Bucket=bucket, Key=meta_key)["Body"].read())
        except Exception:
            pass
        # the noise estimate is only refreshed by a full build; a nightly
        # handful of players is too small a sample to replace it
        if a.all or "noise_sd" not in meta:
            meta["noise_sd"] = round(sd, 3) if sd else None
            meta["noise_pairs"] = n_pairs
        meta.update({"window_minutes": WINDOW, "qual_mp": QUAL_MP,
                     "metric": "bpm", "season_bpm": dist,
                     "league_ts": {k: round(v, 4) for k, v in lg_ts.items()}})
        s3.put_object(Bucket=bucket, Key=meta_key, Body=json.dumps(meta).encode(),
                      ContentType="application/json")
        print("  meta written: noise sd %s, %d seasons of league distribution"
              % (meta["noise_sd"], len(dist)))


if __name__ == "__main__":
    main()
