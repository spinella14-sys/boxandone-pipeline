#!/usr/bin/env python3
"""
compute_ws.py — Win Shares: OWS, DWS, WS and WS/48.

Basketball Reference's method (basketball-reference.com/about/ws.html), built
on Dean Oliver's individual offensive and defensive ratings, from our own box
scores, every season since 1996-97.

  offence   points produced, less 92% of what a league-average offence would
            have scored on the same possessions used
  defence   the share of team possessions he defended, at 108% of league
            points per possession, less his defensive rating (a fifth his own
            stops, four fifths his team's defence)
  wins      each, divided by the marginal points worth a win that season:
            0.32 x league points per game x (team pace / league pace)

Every player's line is computed against the team he played it for, then the
stints are added up, so a traded season is credited to the right context.
Regular season, play-in and playoffs each against their own league averages.

    python3 compute_ws.py check --season 2024-25     # leaders, write nothing
    python3 compute_ws.py build                      # every season
    python3 compute_ws.py build --seasons 2026-27    # the nightly step

Writes v2/models/ws.parquet: player_id, season, season_type, mp, ows, dws, ws,
ws48. Seasons not rebuilt keep their rows.
"""
import argparse
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import parse_assists as PA  # noqa: E402

STATS = ["mp", "fgm", "fga", "fg3m", "fta", "ftm", "orb", "drb", "ast", "stl", "blk", "tov", "pf", "pts"]

PLAYER = """
SELECT b.player_id, b.team_abbr, g.season_type, %s
FROM all_box b JOIN all_games g ON g.game_id = b.game_id
WHERE b.played AND g.season = ? AND upper(COALESCE(g.league, 'NBA')) = 'NBA'
GROUP BY 1, 2, 3
""" % ", ".join(("SUM(COALESCE(b.seconds_played, 0)) / 60.0 AS mp" if k == "mp" else "SUM(COALESCE(b.%s, 0)) AS %s" % (k, k))
                for k in STATS)

TEAM = """
SELECT b.team_abbr, b.opp_abbr, g.season_type, b.game_id, %s
FROM all_box b JOIN all_games g ON g.game_id = b.game_id
WHERE b.played AND g.season = ? AND upper(COALESCE(g.league, 'NBA')) = 'NBA'
GROUP BY 1, 2, 3, 4
""" % ", ".join(("SUM(COALESCE(b.seconds_played, 0)) / 60.0 AS mp" if k == "mp" else "SUM(COALESCE(b.%s, 0)) AS %s" % (k, k))
                for k in STATS)


def d(a, b, z=0.0):
    return a / b if b else z


def poss(t, o):
    """Basketball Reference's team possessions: both sides' estimates, averaged."""
    def one(x, y):
        orbp = d(x["orb"], x["orb"] + y["drb"])
        return x["fga"] + 0.4 * x["fta"] - 1.07 * orbp * (x["fga"] - x["fgm"]) + x["tov"]
    return 0.5 * (one(t, o) + one(o, t))


def teams_for(con, season):
    """{(team, season_type): {team totals, opp_* totals, gp, poss, pace}}."""
    con.execute("CREATE OR REPLACE TEMP TABLE tg AS " + TEAM, [season])
    rows = con.execute("""
        SELECT a.team_abbr, a.season_type, COUNT(*) AS gp, %s, %s
        FROM tg a JOIN tg b ON b.game_id = a.game_id AND b.team_abbr = a.opp_abbr
        GROUP BY 1, 2""" % (", ".join("SUM(a.%s) AS %s" % (k, k) for k in STATS),
                            ", ".join("SUM(b.%s) AS opp_%s" % (k, k) for k in STATS))).fetchall()
    cols = ["team", "stype", "gp"] + STATS + ["opp_" + k for k in STATS]
    out = {}
    for r in rows:
        t = dict(zip(cols, [x if isinstance(x, str) else float(x or 0) for x in r]))
        o = {k: t["opp_" + k] for k in STATS}
        t["poss"] = poss(t, o)
        t["pace"] = 48.0 * d(t["poss"], t["mp"] / 5.0)
        out[(t["team"], t["stype"])] = t
    return out


def league(teams):
    lg = {}
    for (_, st), t in teams.items():
        L = lg.setdefault(st, {"pts": 0.0, "poss": 0.0, "gp": 0.0, "mp": 0.0})
        for k in L:
            L[k] += t[k]
    for L in lg.values():
        L["ppp"] = d(L["pts"], L["poss"])
        L["ppg"] = d(L["pts"], L["gp"])
        L["pace"] = 48.0 * d(L["poss"], L["mp"] / 5.0)
    return lg


def offence(p, t):
    """Points produced and total possessions used (Oliver)."""
    T = t
    opp_trb = T["opp_orb"] + T["opp_drb"]
    mpr = d(p["mp"], T["mp"] / 5.0)
    q_ast = (mpr * 1.14 * d(T["ast"] - p["ast"], T["fgm"])
             + d((T["ast"] / T["mp"]) * p["mp"] * 5 - p["ast"], (T["fgm"] / T["mp"]) * p["mp"] * 5 - p["fgm"]) * (1 - mpr))
    shoot = d(p["pts"] - p["ftm"], 2 * p["fga"])
    fg_part = p["fgm"] * (1 - 0.5 * shoot * q_ast)
    oth_shoot = d((T["pts"] - T["ftm"]) - (p["pts"] - p["ftm"]), 2 * (T["fga"] - p["fga"]))
    ast_part = 0.5 * oth_shoot * p["ast"]
    ftp = d(p["ftm"], p["fta"])
    ft_part = (1 - (1 - ftp) ** 2) * 0.4 * p["fta"] if p["fta"] else 0.0
    tftp = d(T["ftm"], T["fta"])
    t_scposs = T["fgm"] + (1 - (1 - tftp) ** 2) * T["fta"] * 0.4
    t_orbp = d(T["orb"], T["orb"] + (opp_trb - T["opp_orb"]))
    t_play = d(t_scposs, T["fga"] + T["fta"] * 0.4 + T["tov"])
    t_orbw = d((1 - t_orbp) * t_play, (1 - t_orbp) * t_play + t_orbp * (1 - t_play))
    orb_part = p["orb"] * t_orbw * t_play
    orb_cut = 1 - d(T["orb"], t_scposs) * t_orbw * t_play
    sc_poss = (fg_part + ast_part + ft_part) * orb_cut + orb_part
    fgx = (p["fga"] - p["fgm"]) * (1 - 1.07 * t_orbp)
    ftx = ((1 - ftp) ** 2) * 0.4 * p["fta"] if p["fta"] else 0.0
    tot_poss = sc_poss + fgx + ftx + p["tov"]
    pp_fg = 2 * (p["fgm"] + 0.5 * p["fg3m"]) * (1 - 0.5 * shoot * q_ast)
    pp_ast = (2 * d(T["fgm"] - p["fgm"] + 0.5 * (T["fg3m"] - p["fg3m"]), T["fgm"] - p["fgm"])
              * 0.5 * oth_shoot * p["ast"])
    pp_orb = p["orb"] * t_orbw * t_play * d(T["pts"], t_scposs)
    pprod = (pp_fg + pp_ast + p["ftm"]) * orb_cut + pp_orb
    return pprod, tot_poss


def defence(p, t):
    """Individual defensive rating (Oliver)."""
    T = t
    dor = d(T["opp_orb"], T["opp_orb"] + T["drb"])
    dfg = d(T["opp_fgm"], T["opp_fga"])
    fmwt = d(dfg * (1 - dor), dfg * (1 - dor) + (1 - dfg) * dor)
    stops1 = p["stl"] + p["blk"] * fmwt * (1 - 1.07 * dor) + p["drb"] * (1 - fmwt)
    oftp = d(T["opp_ftm"], T["opp_fta"])
    stops2 = ((d(T["opp_fga"] - T["opp_fgm"] - T["blk"], T["mp"]) * fmwt * (1 - 1.07 * dor)
               + d(T["opp_tov"] - T["stl"], T["mp"])) * p["mp"]
              + d(p["pf"], T["pf"]) * 0.4 * T["opp_fta"] * (1 - oftp) ** 2)
    stops = stops1 + stops2
    stop_pct = d(stops * T["mp"], T["poss"] * p["mp"])
    t_drtg = 100 * d(T["opp_pts"], T["poss"])
    d_pts_sc = d(T["opp_pts"], T["opp_fgm"] + (1 - (1 - oftp) ** 2) * T["opp_fta"] * 0.4)
    return t_drtg + 0.2 * (100 * d_pts_sc * (1 - stop_pct) - t_drtg)


def season_ws(con, season):
    teams = teams_for(con, season)
    lg = league(teams)
    rows = con.execute(PLAYER, [season]).fetchall()
    acc = {}
    for r in rows:
        pid, team, st = r[0], r[1], r[2]
        p = dict(zip(STATS, [float(x or 0) for x in r[3:]]))
        t, L = teams.get((team, st)), lg.get(st)
        if not t or not L or p["mp"] <= 0:
            continue
        per_win = 0.32 * L["ppg"] * d(t["pace"], L["pace"], 1.0)
        pprod, tot = offence(p, t)
        ows = (pprod - 0.92 * L["ppp"] * tot) / per_win
        drtg = defence(p, t)
        dws = (p["mp"] / t["mp"]) * t["poss"] * (1.08 * L["ppp"] - drtg / 100.0) / per_win
        a = acc.setdefault((pid, st), {"player_id": pid, "season": season, "season_type": st,
                                       "mp": 0.0, "ows": 0.0, "dws": 0.0})
        a["mp"] += p["mp"]
        a["ows"] += ows
        a["dws"] += dws
    out = []
    for a in acc.values():
        a["ws"] = a["ows"] + a["dws"]
        a["ws48"] = 48.0 * a["ws"] / a["mp"] if a["mp"] else None
        out.append(a)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "build"])
    ap.add_argument("--season")
    ap.add_argument("--seasons")
    a = ap.parse_args()
    import duckdb
    env = PA.load_env()
    s3, bucket = PA.s3c(env), env["R2_BUCKET_NAME"]
    con = PA.connect()
    held = [s for (s,) in con.execute("SELECT DISTINCT season FROM all_games ORDER BY 1").fetchall()]
    spec = a.seasons or a.season
    seasons = spec.split(",") if spec else held
    names = dict(con.execute("SELECT player_id, full_name FROM hot.players").fetchall())
    allrows = []
    for s in seasons:
        if s not in held:
            print("  %s: no games" % s)
            continue
        rows = season_ws(con, s)
        reg = sorted([r for r in rows if r["season_type"] == "regular"], key=lambda r: -r["ws"])
        print("  %s: %d player seasons; leaders %s" % (s, len(rows), "; ".join(
            "%s %.1f" % (names.get(r["player_id"], r["player_id"]), r["ws"]) for r in reg[:3])))
        if a.cmd == "check":
            print("      %-26s %6s %6s %6s %7s %6s" % ("", "OWS", "DWS", "WS", "WS/48", "MP"))
            for r in reg[:20]:
                print("      %-26s %6.1f %6.1f %6.1f %7.3f %6.0f" % (
                    names.get(r["player_id"], r["player_id"])[:26], r["ows"], r["dws"], r["ws"], r["ws48"], r["mp"]))
        allrows += rows
    if a.cmd == "check" or not allrows:
        return
    with tempfile.TemporaryDirectory() as tmp:
        import json
        jl = os.path.join(tmp, "ws.jsonl")
        with open(jl, "w") as f:
            for r in allrows:
                f.write(json.dumps(r) + "\n")
        old = os.path.join(tmp, "old.parquet")
        keep = ""
        try:
            s3.download_file(bucket, "v2/models/ws.parquet", old)
            keep = (" UNION ALL BY NAME SELECT * FROM read_parquet('%s') WHERE season NOT IN (%s)"
                    % (old, ", ".join("'%s'" % s for s in seasons)))
        except Exception:
            pass
        out = os.path.join(tmp, "ws.parquet")
        duckdb.connect().execute(
            "COPY (SELECT player_id, season, season_type, mp, ows, dws, ws, ws48 "
            "FROM read_json_auto('%s', format='newline_delimited')%s) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)"
            % (jl, keep, out))
        s3.upload_file(out, bucket, "v2/models/ws.parquet")
        print("  v2/models/ws.parquet written")


if __name__ == "__main__":
    main()
