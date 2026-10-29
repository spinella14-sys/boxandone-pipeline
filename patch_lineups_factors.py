#!/usr/bin/env python3
"""
patch_lineups_factors.py — run once.

The lineup builder starts counting the box score inside every stint, per side:
  fgm fga fg3m fg3a ftm fta orb drb tov ast stl blk

That is what RAPM needs to split into the four factors (shooting, turnovers,
offensive rebounding, free throws) on both ends, and it gives the game page's
Lineups tab per-unit shooting and box lines.

  build_lineups.py         stints carry home_* / away_* tallies; each game is
                           checked against the box score (FGA, 3PA, FTA, FGM)
  export_game_lineups.py   units and stints carry own (fgm…) and opponent
                           (opp_fgm…) tallies. Older lineup files without the
                           tallies still export; the new columns are blank.

Rebuild afterwards:
    python3 build_lineups.py build --seasons 2019-20:2026-27
    python3 export_game_lineups.py build --seasons 2019-20:2026-27
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))


def patch(name, pairs, marker):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if marker in src:
        print("  %s already patched" % name)
        return
    for old, new, count in pairs:
        n = src.count(old)
        if n != count:
            sys.exit("  %s not as expected (%d× %r) — nothing written" % (name, n, old[:60]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %s patched" % name)


# ---------------------------------------------------------------- build_lineups.py

TALLY = '''
FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")


def tally(st, a, home_id, teams):
    """Count one action into the stint's per-side box score."""
    t = (a.get("actionType") or "").lower()
    tid = a.get("teamId")
    if tid not in teams:
        return
    mine, theirs = (st["fh"], st["fa"]) if tid == home_id else (st["fa"], st["fh"])
    made = (a.get("shotResult") or "") == "Made"
    if t in ("2pt", "3pt"):
        mine["fga"] += 1
        mine["fgm"] += made
        if t == "3pt":
            mine["fg3a"] += 1
            mine["fg3m"] += made
        if made and a.get("assistPersonId"):
            mine["ast"] += 1
        if not made and a.get("blockPersonId"):
            theirs["blk"] += 1
    elif t == "freethrow":
        mine["fta"] += 1
        mine["ftm"] += made
    elif t == "rebound" and a.get("personId"):      # player rebounds, as the box score counts them
        sub = (a.get("subType") or "").lower()
        if sub == "offensive":
            mine["orb"] += 1
        elif sub == "defensive":
            mine["drb"] += 1
    elif t == "turnover":
        mine["tov"] += 1
        if a.get("stealPersonId"):
            theirs["stl"] += 1


def factor_cols(s):
    out = {}
    for side, key in (("home", "fh"), ("away", "fa")):
        for k in FACTORS:
            out["%s_%s" % (side, k)] = int(s.get(key, {}).get(k, 0))
    return out


def walk(gid, pbp, box):'''

BL = [
    ("\ndef walk(gid, pbp, box):", TALLY, 1),
    ('''"s0": tuple(score), "poss_h": 0, "poss_a": 0}''',
     '''"s0": tuple(score), "poss_h": 0, "poss_a": 0,
                "fh": Counter(), "fa": Counter()}''', 1),
    ('''                poss_team = pt
            t = (a.get("actionType") or "").lower()
''', '''                poss_team = pt
            # the box score inside the stint (four-factor RAPM, unit lines)
            tally(st, a, home["teamId"], teams)
            t = (a.get("actionType") or "").lower()
''', 1),
    ('''    return stints, val
''', '''    fm = True
    for key, side in (("fh", home), ("fa", away)):
        s = side.get("statistics") or {}
        for k, b in (("fga", "fieldGoalsAttempted"), ("fgm", "fieldGoalsMade"),
                     ("fg3a", "threePointersAttempted"), ("fta", "freeThrowsAttempted")):
            fm &= sum(x[key][k] for x in stints) == (s.get(b) or 0)
    val["factors_match"] = bool(fm)
    return stints, val
''', 1),
    ('''                    "reliable": val["reliable"]})''',
     '''                    "reliable": val["reliable"], **factor_cols(s)})''', 1),
    ('''        files = {
            "%s.parquet" % season''', '''        fm = [v for v in vals if "factors_match" in v]
        if fm:
            print("      shooting tallies match the box score in %d of %d games"
                  % (sum(1 for v in fm if v["factors_match"]), len(fm)))
        files = {
            "%s.parquet" % season''', 1),
]

# ---------------------------------------------------------------- export_game_lineups.py

LOOP = "    for (gid, per, s0, s1, secs, hids, aids, hp, ap, hpo, apo, rel) in stints:\n"
LOOP_NEW = ("    for row in stints:\n"
            "        (gid, per, s0, s1, secs, hids, aids, hp, ap, hpo, apo, rel) = row[:12]\n"
            "        fh, fa = row[12:12 + len(FACTORS)], row[12 + len(FACTORS):]\n")

XL = [
    ('''def stints_for(con, season, nba_gids=None):''',
     '''FACTORS = ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "orb", "drb", "tov", "ast", "stl", "blk")
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


def stints_for(con, season, nba_gids=None):''', 1),
    ('''    rows = con.execute("""SELECT game_id, period, start_s, end_s, seconds, home_ids, away_ids,
                                 home_pts, away_pts, home_poss, away_poss, reliable
                          FROM read_parquet('%s') %s ORDER BY game_id, period, start_s"""
                       % (path, where)).fetchall()''',
     '''    have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % path).fetchall()}
    fcols = ", ".join(c if c in have else "NULL" for c in
                      ["%s_%s" % (s, k) for s in ("home", "away") for k in FACTORS])
    rows = con.execute("""SELECT game_id, period, start_s, end_s, seconds, home_ids, away_ids,
                                 home_pts, away_pts, home_poss, away_poss, reliable, %s
                          FROM read_parquet('%s') %s ORDER BY game_id, period, start_s"""
                       % (fcols, path, where)).fetchall()''', 1),
    (LOOP, LOOP_NEW, 2),
    # units: accumulate own and opponent tallies
    ('''        for side, mine, theirs, pf, pa, qf, qa in (
                ("home", hids, aids, hp, ap, hpo, apo),
                ("away", aids, hids, ap, hp, apo, hpo)):''',
     '''        for side, mine, theirs, pf, pa, qf, qa, own, opp in (
                ("home", hids, aids, hp, ap, hpo, apo, fh, fa),
                ("away", aids, hids, ap, hp, apo, hpo, fa, fh)):''', 1),
    ('''            u["poss_against"] += qa or 0
''', '''            u["poss_against"] += qa or 0
            for k, v in fx(own, opp).items():
                if v is not None:
                    u[k] = (u.get(k) or 0) + v
                else:
                    u.setdefault(k, None)
''', 1),
    # stint rows: the same, per side
    ('''        for side, mine, pf, pa in (("home", hids, hp, ap), ("away", aids, ap, hp)):''',
     '''        for side, mine, pf, pa, own, opp in (("home", hids, hp, ap, fh, fa),
                                             ("away", aids, ap, hp, fa, fh)):''', 1),
    ('''                        "pts_against": int(pa or 0)})''',
     '''                        "pts_against": int(pa or 0), **fx(own, opp)})''', 1),
    ('''                f.write(json.dumps({c: r[c] for c in COLS}) + "\\n")''',
     '''                f.write(json.dumps({c: r.get(c) for c in COLS + FX}) + "\\n")''', 1),
    ('''                                        "pts_for"::INTEGER "pts_for", "pts_against"::INTEGER "pts_against"
                                 FROM read_json_auto('%s', format='newline_delimited'))
                           TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (sj, sp))''',
     '''                                        "pts_for"::INTEGER "pts_for", "pts_against"::INTEGER "pts_against",
                                        %s
                                 FROM read_json_auto('%s', format='newline_delimited'))
                           TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (FX_CAST, sj, sp))''', 1),
    ('''                                    "unmatched"::INTEGER "unmatched"
                             FROM read_json_auto('%s', format='newline_delimited'))
                       TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (jl, pq))''',
     '''                                    "unmatched"::INTEGER "unmatched", %s
                             FROM read_json_auto('%s', format='newline_delimited'))
                       TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""" % (FX_CAST, jl, pq))''', 1),
]

if __name__ == "__main__":
    patch("build_lineups.py", BL, "def tally(st, a, home_id, teams)")
    patch("export_game_lineups.py", XL, "FX_CAST")
