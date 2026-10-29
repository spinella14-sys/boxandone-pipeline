#!/usr/bin/env python3
"""
export_game_lineups.py — every five-man unit each team used in a game.

Reads the season's stints from build_lineups.py (data/lineups/{season}.parquet)
and writes v2/games/{game_id}/lineups.parquet under OUR game id, next to the
game's pbp.parquet, so the game page fetches one small file.

One row per team and unit:
  side            home / away
  player_ids      our ids, comma separated (nba_ids alongside)
  seconds, stints
  pts_for, pts_against, poss_for, poss_against
  started         the unit that opened the game
  closed          the unit on the floor at the final horn
  reliable        the game passed build_lineups' checks

A player's own minutes and plus-minus in the game are the sum of the units he
was in, so the page derives them rather than this file storing them twice.

    python3 export_game_lineups.py check  --season 2025-26     # mapping coverage, writes nothing
    python3 export_game_lineups.py build  --seasons 2019-20:2026-27
    python3 export_game_lineups.py build  --games ID,ID,...     # our ids — the nightly job
"""
import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
LINEUPS = os.path.join(HOME, "data", "lineups")


ARCHIVE = os.path.join(HOME, "data", "archive")


def maps():
    """NBA game id -> ours, and NBA person id -> ours.

    Completed seasons' game mappings live in the archive cache
    (data/archive/{season}/game_identifiers.parquet), not the hot database.
    Any game still missing is matched on date and teams from NBA's own box
    score (gameCode "20191022/NOPTOR") against our games index."""
    import duckdb
    import glob
    con = duckdb.connect(DB_PATH, read_only=True)
    games = dict(con.execute("""SELECT source_game_id, game_id FROM game_identifiers
                                WHERE source='nba'""").fetchall())
    files = glob.glob(os.path.join(ARCHIVE, "*", "game_identifiers.parquet"))
    if files:
        for nba, ours in con.execute(
                "SELECT source_game_id, game_id FROM read_parquet(?, union_by_name=true) "
                "WHERE source='nba'", [files]).fetchall():
            games.setdefault(nba, ours)
    ids = {int(s): p for s, p in con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall()
        if str(s).isdigit()}
    by_key = {}
    gfiles = glob.glob(os.path.join(ARCHIVE, "*", "games.parquet"))
    q = "SELECT game_id, CAST(game_date AS VARCHAR), home_abbr, away_abbr FROM games"
    rows = con.execute(q).fetchall()
    if gfiles:
        rows += con.execute(q.replace("FROM games", "FROM read_parquet(?, union_by_name=true)"),
                            [gfiles]).fetchall()
    con.close()
    import bridge_games as BG
    for gid, d, h, a in rows:
        by_key[(str(d)[:10], BG.norm_team(h), BG.norm_team(a))] = gid
    return games, ids, by_key


def match_by_box(nba_gid, by_key):
    import cdn_feed as C
    box = C.load(nba_gid, "box", fetch=False)
    g = (box or {}).get("game") or {}
    code = g.get("gameCode") or ""
    if "/" not in code:
        return None
    d = code[:8]
    d = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
    import bridge_games as BG
    h = BG.norm_team((g.get("homeTeam") or {}).get("teamTricode"))
    a = BG.norm_team((g.get("awayTeam") or {}).get("teamTricode"))
    if (d, h, a) in by_key:
        return by_key[(d, h, a)], False
    # the 2020 bubble Finals: our index and NBA's disagree on who was "home"
    if (d, a, h) in by_key:
        return by_key[(d, a, h)], True
    return None


FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
FX = list(FACTORS) + ["opp_" + k for k in FACTORS]
FX_CAST = ", ".join('"%s"::INTEGER "%s"' % (c, c) for c in FX)


def fx(own, opp):
    """Own and opponent tallies as a dict; blank when the lineup file predates them."""
    out = {}
    for k, v in zip(FACTORS, own or ()):
        out[k] = v
    for k, v in zip(FACTORS, opp or ()):
        out["opp_" + k] = v
    return out


def stints_for(con, season, nba_gids=None):
    path = os.path.join(LINEUPS, "%s.parquet" % season)
    if not os.path.exists(path):
        print("  %s: no lineups on file — build_lineups.py build --seasons %s first" % (season, season))
        return {}
    where = ""
    if nba_gids:
        where = "WHERE game_id IN (%s)" % ",".join("'%s'" % g for g in nba_gids)
    have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % path).fetchall()}
    fcols = ", ".join(c if c in have else "NULL" for c in
                      ["%s_%s" % (s, k) for s in ("home", "away") for k in FACTORS])
    rows = con.execute("""SELECT game_id, period, start_s, end_s, seconds, home_ids, away_ids,
                                 home_pts, away_pts, home_poss, away_poss, reliable, %s
                          FROM read_parquet('%s') %s ORDER BY game_id, period, start_s"""
                       % (fcols, path, where)).fetchall()
    out = defaultdict(list)
    for r in rows:
        out[r[0]].append(r)
    return out


def ukey(s):
    return ",".join(sorted((x for x in (s or "").split(",") if x), key=int))


def units(stints, ids):
    """Aggregate one game's stints into rows per (side, unit)."""
    agg = {}
    first = stints[0]
    last = stints[-1]
    for row in stints:
        (gid, per, s0, s1, secs, hids, aids, hp, ap, hpo, apo, rel) = row[:12]
        fh, fa = row[12:12 + len(FACTORS)], row[12 + len(FACTORS):]
        for side, mine, theirs, pf, pa, qf, qa, own, opp in (
                ("home", hids, aids, hp, ap, hpo, apo, fh, fa),
                ("away", aids, hids, ap, hp, apo, hpo, fa, fh)):
            key = (side, ukey(mine))
            u = agg.setdefault(key, {"side": side, "seconds": 0, "stints": 0, "pts_for": 0,
                                     "pts_against": 0, "poss_for": 0, "poss_against": 0,
                                     "reliable": bool(rel)})
            u["seconds"] += secs or 0
            u["stints"] += 1
            u["pts_for"] += pf or 0
            u["pts_against"] += pa or 0
            u["poss_for"] += qf or 0
            u["poss_against"] += qa or 0
            for k, v in fx(own, opp).items():
                if v is not None:
                    u[k] = (u.get(k) or 0) + v
                else:
                    u.setdefault(k, None)
    opened = {("home", ukey(first[5])),
              ("away", ukey(first[6]))}
    closed = {("home", ukey(last[5])),
              ("away", ukey(last[6]))}
    rows = []
    for (side, unit), u in agg.items():
        nba = [int(x) for x in unit.split(",") if x]
        ours = [ids.get(x) for x in nba]
        rows.append(dict(u, nba_ids=",".join(str(x) for x in nba),
                         player_ids=",".join(p or "" for p in ours),
                         unmatched=sum(1 for p in ours if not p),
                         started=(side, unit) in opened, closed=(side, unit) in closed))
    rows.sort(key=lambda r: (r["side"], -r["seconds"]))
    return rows


def stint_rows(stints, ids, flip):
    """One row per side per stint, under our ids, for the rotation chart."""
    out = []
    for row in stints:
        (gid, per, s0, s1, secs, hids, aids, hp, ap, hpo, apo, rel) = row[:12]
        fh, fa = row[12:12 + len(FACTORS)], row[12 + len(FACTORS):]
        for side, mine, pf, pa, own, opp in (("home", hids, hp, ap, fh, fa),
                                             ("away", aids, ap, hp, fa, fh)):
            if flip:
                side = "away" if side == "home" else "home"
            ours = [ids.get(int(x)) or "" for x in (mine or "").split(",") if x]
            out.append({"side": side, "start_s": int(s0 or 0), "end_s": int(s1 or 0),
                        "player_ids": ",".join(ours), "pts_for": int(pf or 0),
                        "pts_against": int(pa or 0), **fx(own, opp)})
    return out


COLS = ["side", "player_ids", "nba_ids", "seconds", "stints", "pts_for", "pts_against",
        "poss_for", "poss_against", "started", "closed", "reliable", "unmatched"]


def write_one(con_factory, s3, bucket, our_gid, rows, srows=None):
    con = con_factory()
    with tempfile.TemporaryDirectory() as tmp:
        if srows:
            sj = os.path.join(tmp, "s.jsonl")
            with open(sj, "w") as f:
                for r in srows:
                    f.write(json.dumps(r) + "\n")
            sp = os.path.join(tmp, "s.parquet")
            con.execute("""COPY (SELECT "side"::VARCHAR "side", "start_s"::INTEGER "start_s",
                                        "end_s"::INTEGER "end_s", "player_ids"::VARCHAR "player_ids",
                                        "pts_for"::INTEGER "pts_for", "pts_against"::INTEGER "pts_against",
                                        %s
                                 FROM read_json_auto('%s', format='newline_delimited'))
                           TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (FX_CAST, sj, sp))
            if s3:
                s3.upload_file(sp, bucket, "v2/games/%s/stints.parquet" % our_gid,
                               ExtraArgs={"ContentType": "application/octet-stream"})
        jl = os.path.join(tmp, "r.jsonl")
        with open(jl, "w") as f:
            for r in rows:
                f.write(json.dumps({c: r.get(c) for c in COLS + FX}) + "\n")
        pq = os.path.join(tmp, "l.parquet")
        con.execute("""COPY (SELECT "side"::VARCHAR "side", "player_ids"::VARCHAR "player_ids",
                                    "nba_ids"::VARCHAR "nba_ids", "seconds"::INTEGER "seconds",
                                    "stints"::INTEGER "stints", "pts_for"::INTEGER "pts_for",
                                    "pts_against"::INTEGER "pts_against", "poss_for"::INTEGER "poss_for",
                                    "poss_against"::INTEGER "poss_against", "started"::BOOLEAN "started",
                                    "closed"::BOOLEAN "closed", "reliable"::BOOLEAN "reliable",
                                    "unmatched"::INTEGER "unmatched", %s
                             FROM read_json_auto('%s', format='newline_delimited'))
                       TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (FX_CAST, jl, pq))
        if s3:
            s3.upload_file(pq, bucket, "v2/games/%s/lineups.parquet" % our_gid,
                           ExtraArgs={"ContentType": "application/octet-stream"})
    con.close()


def run(plan, upload):
    """plan: {season: [nba_gid, ...] or None for all}"""
    import duckdb
    nba_to_ours, ids, by_key = maps()
    s3 = bucket = None
    if upload:
        import nightly2 as N
        env = N.load_env()
        s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    for season, only in plan.items():
        games = stints_for(con, season, only)
        if not games:
            continue
        jobs, unmapped, unreliable, nobody = [], [], 0, 0
        for nba_gid, st in games.items():
            ours, flip = nba_to_ours.get(nba_gid), False
            if not ours:
                ours, flip = match_by_box(nba_gid, by_key) or (None, False)
            if not ours:
                unmapped.append(nba_gid)
                continue
            rows = units(st, ids)
            if flip:
                for r in rows:
                    r["side"] = "away" if r["side"] == "home" else "home"
            unreliable += not rows[0]["reliable"]
            nobody += sum(r["unmatched"] for r in rows)
            jobs.append((ours, rows, stint_rows(st, ids, flip)))
        if upload:
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(lambda j: write_one(duckdb.connect, s3, bucket, *j), jobs))
        print("  %s: %d games %s, %d not mapped to our ids, %d set aside as unreliable, "
              "%d unit-slots without our player id"
              % (season, len(jobs), "written" if upload else "ready", len(unmapped),
                 unreliable, nobody))
        if unmapped:
            print("      unmapped NBA ids: %s%s" % (", ".join(unmapped[:8]),
                                                   " …" if len(unmapped) > 8 else ""))
        if jobs and not upload:
            gid, rows, _ = jobs[0]
            print("      e.g. %s: %d units; most-used home unit %ss, +%d"
                  % (gid, len(rows), rows[0]["seconds"], rows[0]["pts_for"] - rows[0]["pts_against"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    ap.add_argument("--games", help="our game ids, comma separated")
    a = ap.parse_args()
    import build_lineups as B
    if a.games:
        nba_to_ours, _, _ = maps()
        ours_to_nba = {v: k for k, v in nba_to_ours.items()}
        plan = defaultdict(list)
        for g in a.games.split(","):
            nba = ours_to_nba.get(g.strip())
            if not nba:
                continue
            yy = int(nba[3:5])
            y = 2000 + yy if yy < 90 else 1900 + yy
            plan["%d-%s" % (y, str(y + 1)[2:])].append(nba)
        if not plan:
            print("  none of those games has an NBA id yet — nothing to write")
            return
    else:
        spec = a.seasons or a.season or B.current_season()
        plan = {s: None for s in B.seasons_from(spec)}
    run(dict(plan), upload=(a.cmd == "build"))


if __name__ == "__main__":
    main()
