#!/usr/bin/env python3
"""
build_lineups.py — lineups from NBA's CDN play-by-play, 2019-20 on.

The CDN feed's substitutions reconcile (lineup_check.py: 61 of 63 sampled
games perfect). From them this builds every stint — a stretch with the same
ten players on the floor — and from the stints, per-player measures.

  fetch   the CDN play-by-play and box score for every game of a season,
          cached under raw/pbp_cdn/ (resumable; a game id that does not exist
          is remembered and not asked for again)
  build   stints, a validation row per game, player measures; written to
          data/lineups/ and uploaded to R2 under v2/lineups/
  check   the F/C test: do the lineup measures separate Adam's labelled Fs
          from his Cs?

    python3 build_lineups.py fetch --seasons 2019-20:2025-26
    python3 build_lineups.py build --seasons 2019-20:2025-26
    python3 build_lineups.py check --season 2025-26

Lineups. Period 1 starts from the box score's starters. Later periods are not
listed in the feed, so each is inferred: whoever acts, or is subbed out,
before being subbed in was on the floor when it began. If that finds fewer
than five, the gap is filled from the previous period's closing lineup,
choosing whoever has the most box-score minutes not yet accounted for.

Possessions. Every CDN action names the team with the ball. A possession
starts each time that changes to a team (and at each period start), and is
credited to the stint it starts in.

Validation. A game is reliable only if no event is credited to a player off
the floor, each side always has five, stint points add up to the final score
and nearly every player's minutes match the box score. Unreliable games are
written with their reasons and left out of every player measure.

Player measures, per player-season, reliable games only:
  minutes, on-court points and possessions for and against, on/off ratings
  with_c_share     share of his minutes with a teammate Adam rated C on the
                   floor (coverage: share of those minutes where all four
                   teammates have a position)
  tallest_share    share of his minutes as his side's tallest player (ties
                   count; heights on file for all five)
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import cdn_feed as C  # noqa: E402

DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
OUT = os.path.join(HOME, "data", "lineups")
MISSING = os.path.join(C.RAW, "missing.txt")
PLAYER_EVENTS = {"2pt", "3pt", "freethrow", "rebound", "turnover", "steal", "block", "foul", "jumpball"}


# ------------------------------------------------------------------ games

def seasons_from(spec):
    lo, hi = spec.split(":") if ":" in spec else (spec, spec)
    a, b = int(lo[:4]), int(hi[:4])
    return ["%d-%s" % (y, str(y + 1)[2:]) for y in range(min(a, b), max(a, b) + 1)]


def candidate_ids(season):
    """Every id NBA could have used: regular season, play-in, playoffs."""
    yy = season[2:4]
    ids = ["002%s0%04d" % (yy, n) for n in range(1, 1231)]
    ids += ["005%s00%d%d1" % (yy, r, s) for r in (1, 2) for s in range(4)]
    series = {1: 8, 2: 4, 3: 2, 4: 1}
    ids += ["004%s00%d%d%d" % (yy, r, s, g) for r, n in series.items()
            for s in range(n) for g in range(1, 8)]
    return ids


def load_missing():
    try:
        return set(open(MISSING).read().split())
    except OSError:
        return set()


def current_season():
    from datetime import date
    d = date.today()
    y = d.year if d.month >= 10 else d.year - 1
    return "%d-%s" % (y, str(y + 1)[2:])


def cmd_fetch(seasons):
    missing = load_missing()
    os.makedirs(C.RAW, exist_ok=True)
    for season in seasons:
        live = season == current_season()
        ids = [g for g in candidate_ids(season) if g not in missing]
        streak = 0
        have = new = gone = failed = 0
        t0 = time.time()
        for i, gid in enumerate(ids, 1):
            if os.path.exists(C._cache(gid, "pbp")) and os.path.exists(C._cache(gid, "box")):
                have += 1
                continue
            try:
                if live and gid[2] == "2" and streak >= 15:
                    continue            # past the last game played so far
                pbp = C.load(gid, "pbp")
                if not pbp:
                    # in the season under way, an absent id is usually a game
                    # not played yet: never remember it as missing
                    if live:
                        streak += gid[2] == "2"
                    else:
                        missing.add(gid)
                    gone += 1
                    continue
                streak = 0
                C.load(gid, "box")
            except Exception as e:             # cdn_feed.FetchFailed: try again next run
                failed += 1
                print("    skipped for now: %s" % str(e)[:100], flush=True)
                continue
            new += 1
            time.sleep(0.25)
            if i % 100 == 0:
                open(MISSING, "w").write("\n".join(sorted(missing)))
                print("    %s  %d/%d  new %d  cached %d  absent %d  %.0fs"
                      % (season, i, len(ids), new, have, gone, time.time() - t0), flush=True)
        open(MISSING, "w").write("\n".join(sorted(missing)))
        print("  %s: %d games on file (%d new), %d ids absent, %d to retry"
              % (season, have + new, new, gone, failed))


# ---------------------------------------------------------------- lineups

def secs(clock):
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def actors(a):
    t = (a.get("actionType") or "").lower()
    out = []
    if t in PLAYER_EVENTS and a.get("personId"):
        if not (t == "foul" and "technical" in (a.get("subType") or "").lower()):
            out.append(a["personId"])
    for k in ("assistPersonId", "stealPersonId", "blockPersonId"):
        if a.get(k):
            out.append(a[k])
    return out


def walk(gid, pbp, box):
    """-> (stints, validation dict)"""
    g = box["game"]
    home, away = g["homeTeam"], g["awayTeam"]
    team_of, starters, box_min = {}, defaultdict(set), {}
    for side in (home, away):
        for p in side["players"]:
            team_of[p["personId"]] = side["teamId"]
            if str(p.get("starter")) == "1":
                starters[side["teamId"]].add(p["personId"])
            box_min[p["personId"]] = (secs((p.get("statistics") or {}).get("minutes")) or 0) / 60.0
    teams = [home["teamId"], away["teamId"]]
    acts = sorted(pbp["game"]["actions"], key=lambda a: (a.get("orderNumber") or 0))
    periods = sorted({a["period"] for a in acts if a.get("period")})

    v = Counter()
    played = defaultdict(float)
    stints = []
    prev_close = {t: set(starters[t]) for t in teams}
    score = [0, 0]                                   # home, away

    def new_stint(per, at, on):
        return {"period": per, "start": at, "end": at,
                "home": tuple(sorted(on[home["teamId"]])), "away": tuple(sorted(on[away["teamId"]])),
                "s0": tuple(score), "poss_h": 0, "poss_a": 0}

    def close(st, at):
        st["end"] = at
        st["pts_h"] = score[0] - st["s0"][0]
        st["pts_a"] = score[1] - st["s0"][1]
        if st["end"] > st["start"] or st["pts_h"] or st["pts_a"] or st["poss_h"] or st["poss_a"]:
            stints.append(st)

    for per in periods:
        pa = [a for a in acts if a.get("period") == per]
        length = 720 if per <= 4 else 300
        base = 4 * 720 + (per - 5) * 300 if per > 4 else (per - 1) * 720
        # early 2019-20 box scores often lack starter flags; then period 1
        # is inferred the same way as every later period
        if per == 1 and all(len(starters[t]) == 5 for t in teams):
            on = {t: set(starters[t]) for t in teams}
        else:
            on = {t: set() for t in teams}
            subbed_in = set()
            for a in pa:
                t = (a.get("actionType") or "").lower()
                if t == "substitution":
                    pid, tid = a.get("personId"), a.get("teamId")
                    if (a.get("subType") or "").lower() == "in":
                        subbed_in.add(pid)
                    elif pid not in subbed_in and tid in on and len(on[tid]) < 5:
                        on[tid].add(pid)
                    continue
                for pid in actors(a):
                    tid = team_of.get(pid)
                    if tid in on and pid not in subbed_in and len(on[tid]) < 5:
                        on[tid].add(pid)
            for tid in teams:
                if len(on[tid]) < 5:
                    # someone played the whole period without a recorded event
                    cands = [p for p in prev_close[tid] if p not in on[tid] and p not in subbed_in]
                    cands.sort(key=lambda p: -(box_min.get(p, 0) - played.get(p, 0)))
                    for p in cands[:5 - len(on[tid])]:
                        on[tid].add(p)
                        v["period_start_filled"] += 1
                if len(on[tid]) != 5:
                    v["period_start_not_5"] += 1

        last = length
        poss_team = None
        st = new_stint(per, base, on)
        for a in pa:
            now = secs(a.get("clock"))
            if now is not None and now <= last:
                for tid in teams:
                    for pid in on[tid]:
                        played[pid] += (last - now) / 60.0
                last = now
            at = base + (length - last)
            # scores
            try:
                sh, sa = int(a.get("scoreHome") or score[0]), int(a.get("scoreAway") or score[1])
                score[0], score[1] = sh, sa
            except (TypeError, ValueError):
                pass
            # possessions: a change of the team with the ball starts one
            pt = a.get("possession")
            if pt in teams and pt != poss_team:
                if pt == home["teamId"]:
                    st["poss_h"] += 1
                else:
                    st["poss_a"] += 1
                poss_team = pt
            t = (a.get("actionType") or "").lower()
            if t == "substitution":
                pid, tid = a.get("personId"), a.get("teamId")
                if tid not in on:
                    continue
                close(st, at)
                if (a.get("subType") or "").lower() == "out":
                    if pid not in on[tid]:
                        v["sub_out_not_on"] += 1
                    on[tid].discard(pid)
                else:
                    on[tid].add(pid)
                st = new_stint(per, at, on)
                continue
            for pid in actors(a):
                tid = team_of.get(pid)
                if tid in on:
                    v["events"] += 1
                    if pid not in on[tid]:
                        v["off_floor"] += 1
            for tid in teams:
                v["checks"] += 1
                if len(on[tid]) != 5:
                    v["not_5"] += 1
        if last > 0:
            for tid in teams:
                for pid in on[tid]:
                    played[pid] += last / 60.0
        close(st, base + length)
        prev_close = {t: set(on[t]) for t in teams}

    for pid, m in box_min.items():
        if m > 0 or played.get(pid, 0) > 0:
            v["players"] += 1
            v["minutes_match"] += abs(played.get(pid, 0) - m) <= 0.5
    final = (home.get("score"), away.get("score"))
    v["points_match"] = int(sum(s["pts_h"] for s in stints) == final[0]
                            and sum(s["pts_a"] for s in stints) == final[1])
    reasons = []
    if v["off_floor"]:
        reasons.append("%d events off the floor" % v["off_floor"])
    if v["not_5"]:
        reasons.append("not five a side at %d events" % v["not_5"])
    if v["players"] and v["minutes_match"] / v["players"] < 0.9:
        reasons.append("minutes match %d/%d" % (v["minutes_match"], v["players"]))
    if not v["points_match"]:
        reasons.append("stint points do not add to the final score")
    est = {}
    for key, side in (("h", home), ("a", away)):
        s = side.get("statistics") or {}
        est[key] = ((s.get("fieldGoalsAttempted") or 0) + 0.44 * (s.get("freeThrowsAttempted") or 0)
                    - (s.get("reboundsOffensive") or 0) + (s.get("turnovers") or 0))
    val = {"game_id": gid, "home": home["teamTricode"], "away": away["teamTricode"],
           "home_id": home["teamId"], "away_id": away["teamId"],
           "events": v["events"], "off_floor": v["off_floor"], "not_five": v["not_5"],
           "minutes_match": v["minutes_match"], "players": v["players"],
           "period_start_filled": v["period_start_filled"],
           "poss_h": sum(s["poss_h"] for s in stints), "poss_a": sum(s["poss_a"] for s in stints),
           "poss_est_h": round(est["h"], 1), "poss_est_a": round(est["a"], 1),
           "reliable": not reasons, "reasons": "; ".join(reasons)}
    return stints, val


# ---------------------------------------------------------------- build

def context():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    ids = {int(s): p for s, p in con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall()
        if str(s).isdigit()}
    has_bio = con.execute("""SELECT COUNT(*) FROM duckdb_tables()
                             WHERE table_name='player_bio'""").fetchone()[0] > 0
    hsql = ("SELECT p.player_id, COALESCE(p.height_in, b.height_in) FROM players p "
            "LEFT JOIN player_bio b ON b.player_id=p.player_id AND b.source='nba'"
            if has_bio else "SELECT player_id, height_in FROM players")
    heights = {p: h for p, h in con.execute(hsql).fetchall() if h}
    pos = {}
    try:
        pos = dict(con.execute("""SELECT player_id, position FROM player_positions
                                  WHERE source='admin'""").fetchall())
    except Exception:
        print("  no admin positions in the database — run sync_positions_now.py first")
    con.close()
    return ids, heights, pos


def player_measures(season, stints, vals, ids, heights, pos):
    reliable = {v["game_id"] for v in vals if v["reliable"]}
    side_team = {}
    for v in vals:
        side_team[(v["game_id"], "h")] = v["home"]
        side_team[(v["game_id"], "a")] = v["away"]
    m = defaultdict(lambda: defaultdict(float))
    team_tot = defaultdict(lambda: defaultdict(float))
    unresolved = Counter()
    for s in stints:
        if s["game_id"] not in reliable:
            continue
        dur = (s["end"] - s["start"]) / 60.0
        for key, opp in (("h", "a"), ("a", "h")):
            team = side_team[(s["game_id"], key)]
            five = s["home"] if key == "h" else s["away"]
            pf, pa = s["pts_" + key], s["pts_" + opp]
            qf, qa = s["poss_" + key], s["poss_" + opp]
            tt = team_tot[team]
            tt["min"] += dur; tt["pf"] += pf; tt["pa"] += pa; tt["qf"] += qf; tt["qa"] += qa
            pids = [ids.get(x) for x in five]
            for x, pid in zip(five, pids):
                if not pid:
                    unresolved[x] += dur
                    continue
                r = m[(pid, team)]
                r["min"] += dur; r["pf"] += pf; r["pa"] += pa; r["qf"] += qf; r["qa"] += qa
                mates = [q for q in pids if q and q != pid]
                if len(mates) == 4 and all(q in pos for q in mates):
                    r["pos_cov"] += dur
                    if any(pos[q] == "C" for q in mates):
                        r["with_c"] += dur
                hs = [heights.get(q) for q in pids]
                if all(hs) and len(hs) == 5:
                    r["h_cov"] += dur
                    if heights.get(pid) == max(hs):
                        r["tallest"] += dur
    rows = []
    for (pid, team), r in m.items():
        tt = team_tot[team]
        off = {k: tt[k] - r[k] for k in ("min", "pf", "pa", "qf", "qa")}
        on_net = 100 * (r["pf"] / r["qf"] - r["pa"] / r["qa"]) if r["qf"] and r["qa"] else None
        off_net = (100 * (off["pf"] / off["qf"] - off["pa"] / off["qa"])
                   if off["qf"] > 20 and off["qa"] > 20 else None)
        rows.append({
            "player_id": pid, "season": season, "team_abbr": team,
            "minutes": round(r["min"], 1),
            "pts_for": r["pf"], "pts_against": r["pa"], "poss_for": r["qf"], "poss_against": r["qa"],
            "on_ortg": round(100 * r["pf"] / r["qf"], 1) if r["qf"] else None,
            "on_drtg": round(100 * r["pa"] / r["qa"], 1) if r["qa"] else None,
            "on_net": round(on_net, 1) if on_net is not None else None,
            "off_net": round(off_net, 1) if off_net is not None else None,
            "on_off": round(on_net - off_net, 1) if on_net is not None and off_net is not None else None,
            "with_c_share": round(r["with_c"] / r["pos_cov"], 3) if r["pos_cov"] else None,
            "pos_coverage": round(r["pos_cov"] / r["min"], 3) if r["min"] else None,
            "tallest_share": round(r["tallest"] / r["h_cov"], 3) if r["h_cov"] else None,
            "height_coverage": round(r["h_cov"] / r["min"], 3) if r["min"] else None,
        })
    return rows, unresolved


def write_parquet(con, rows, cols, path):
    """Rows of dicts -> Parquet, through a JSON-lines file DuckDB reads."""
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in rows:
            f.write(json.dumps({c: r.get(c) for c in cols}) + "\n")
        tmp = f.name
    try:
        if rows:
            con.execute("COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
                        "TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % (tmp, path))
    finally:
        os.remove(tmp)


def cmd_build(seasons, upload=True):
    import duckdb
    os.makedirs(OUT, exist_ok=True)
    ids, heights, pos = context()
    s3 = bucket = None
    if upload:
        import nightly2 as N
        env = N.load_env()
        s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    con = duckdb.connect()
    missing = load_missing()
    for season in seasons:
        stints, vals = [], []
        for gid in candidate_ids(season):
            if gid in missing:
                continue
            pbp, box = C.load(gid, "pbp", fetch=False), C.load(gid, "box", fetch=False)
            if not pbp or not box:
                continue
            try:
                st, val = walk(gid, pbp, box)
            except Exception as e:
                vals.append({"game_id": gid, "reliable": False, "reasons": "parse error: %s" % str(e)[:80]})
                continue
            val["season"] = season
            vals.append(val)
            for s in st:
                stints.append({
                    "season": season, "game_id": gid, "period": s["period"],
                    "start_s": s["start"], "end_s": s["end"], "seconds": s["end"] - s["start"],
                    "home": val["home"], "away": val["away"],
                    "home_ids": ",".join(str(x) for x in s["home"]),
                    "away_ids": ",".join(str(x) for x in s["away"]),
                    "home_pts": s["pts_h"], "away_pts": s["pts_a"],
                    "home_poss": s["poss_h"], "away_poss": s["poss_a"],
                    "reliable": val["reliable"]})
        if not vals:
            print("  %s: no games on file — run fetch first" % season)
            continue
        raw = [dict(s, home=tuple(int(x) for x in s["home_ids"].split(",") if x),
                    away=tuple(int(x) for x in s["away_ids"].split(",") if x),
                    start=s["start_s"], end=s["end_s"], pts_h=s["home_pts"], pts_a=s["away_pts"],
                    poss_h=s["home_poss"], poss_a=s["away_poss"]) for s in stints]
        players, unresolved = player_measures(season, raw, [v for v in vals if "home" in v],
                                              ids, heights, pos)
        rel = sum(1 for v in vals if v["reliable"])
        pr = [v for v in vals if v.get("reliable") and v.get("poss_est_h")]
        ratio = (sum(v["poss_h"] + v["poss_a"] for v in pr) /
                 max(1, sum(v["poss_est_h"] + v["poss_est_a"] for v in pr)))
        print("  %s: %d games, %d reliable (%.1f%%), %d stints, %d player-teams, "
              "possessions %.3f x box estimate, %.0f min of unbridged players"
              % (season, len(vals), rel, 100.0 * rel / len(vals), len(stints), len(players),
                 ratio, sum(unresolved.values())))
        files = {
            "%s.parquet" % season: (stints, list(stints[0].keys()) if stints else []),
            "%s_games.parquet" % season: (vals, sorted({k for v in vals for k in v})),
            "%s_players.parquet" % season: (players, list(players[0].keys()) if players else []),
        }
        for name, (rows, cols) in files.items():
            path = os.path.join(OUT, name)
            if os.path.exists(path):
                os.remove(path)
            write_parquet(con, rows, cols, path)
            if s3:
                s3.upload_file(path, bucket, "v2/lineups/%s" % name)
        bad = [v for v in vals if not v["reliable"]]
        for v in bad[:5]:
            print("      set aside %s: %s" % (v["game_id"], v["reasons"]))
        if len(bad) > 5:
            print("      … and %d more (see %s_games.parquet)" % (len(bad) - 5, season))


# ---------------------------------------------------------------- check

def cmd_check(season):
    import duckdb
    path = os.path.join(OUT, "%s_players.parquet" % season)
    if not os.path.exists(path):
        sys.exit("  build %s first" % season)
    _, _, pos = context()
    con = duckdb.connect()
    rows = con.execute("""SELECT player_id, SUM(minutes) m,
                                 SUM(with_c_share * minutes * pos_coverage) / NULLIF(SUM(minutes * pos_coverage), 0) wc,
                                 SUM(tallest_share * minutes * height_coverage) / NULLIF(SUM(minutes * height_coverage), 0) tl
                          FROM read_parquet('%s') GROUP BY 1 HAVING SUM(minutes) >= 500""" % path).fetchall()
    for label, idx in (("share of minutes beside a C", 2), ("share of minutes as tallest", 3)):
        print("\n  %s (players with 500+ minutes)" % label)
        for p in ("PG", "CG", "W", "F", "C"):
            vals = sorted(r[idx] for r in rows if pos.get(r[0]) == p and r[idx] is not None)
            if vals:
                q = lambda f: vals[min(len(vals) - 1, int(f * len(vals)))]
                print("    %-3s n=%-4d  25th %.2f  median %.2f  75th %.2f" % (p, len(vals), q(.25), q(.5), q(.75)))
    # how well does "beside a C" alone split F from C?
    fc = [(r[2], pos[r[0]]) for r in rows if pos.get(r[0]) in ("F", "C") and r[2] is not None]
    # both measures together: C when rarely beside another C, or usually tallest
    fc2 = [(r[2], r[3], pos[r[0]]) for r in rows
           if pos.get(r[0]) in ("F", "C") and r[2] is not None and r[3] is not None]
    if fc2:
        grid = [i / 20 for i in range(1, 20)]
        best2 = max((sum(((w < a) or (t > b)) == (p == "C") for w, t, p in fc2), a, b)
                    for a in grid for b in grid)
        print("  F vs C on both: C if beside a C < %.2f or tallest > %.2f gets %d of %d right (%.0f%%)"
              % (best2[1], best2[2], best2[0], len(fc2), 100.0 * best2[0] / len(fc2)))
        miss = [(r, pos[r[0]]) for r in rows if pos.get(r[0]) in ("F", "C") and r[2] is not None
                and r[3] is not None and (((r[2] < best2[1]) or (r[3] > best2[2])) != (pos[r[0]] == "C"))]
        import duckdb as _d
        nm = dict(_d.connect(DB_PATH, read_only=True).execute(
            "SELECT player_id, full_name FROM players").fetchall())
        for r, p in miss[:20]:
            print("    missed: %-24s you: %s  beside a C %.2f  tallest %.2f" % (nm.get(r[0], r[0])[:24], p, r[2], r[3]))
    if fc:
        best = max(((sum((v < c) == (p == "C") for v, p in fc), c) for c in [i / 100 for i in range(5, 95)]))
        print("\n  F vs C on 'beside a C' alone: best cut %.2f gets %d of %d right (%.0f%%)"
              % (best[1], best[0], len(fc), 100.0 * best[0] / len(fc)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "build", "check"])
    ap.add_argument("--seasons", default="2019-20:2025-26")
    ap.add_argument("--season", default="2025-26")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    if a.cmd == "fetch":
        cmd_fetch(seasons_from(a.seasons))
    elif a.cmd == "build":
        cmd_build(seasons_from(a.seasons), upload=not a.no_upload)
    else:
        cmd_check(a.season)


if __name__ == "__main__":
    main()
