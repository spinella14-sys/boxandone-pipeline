#!/usr/bin/env python3
"""
patch_standings.py — add wins, losses and conference to team_season.

A standings view needs records, and team_season has none. games carries
home_abbr, away_abbr, home_score and away_score, so W/L is derivable: unpivot
each game into two rows (one per side) and compare scores.

Conference comes from a static map that includes historical abbreviations —
SEA and OKC are the same franchise in the West, CHH/CHA/CHO are East, and the
New Orleans codes (NOH/NOK/NOP) are West. Divisions are deliberately left out:
the NBA went from four divisions to six in 2004-05, so a division column would
mean different things in different halves of your 30 seasons. Conference has
been stable.

Also adds point differential, which is what actually sorts a standings table
once records tie.

Run:  python3 patch_standings.py
Then: python3 export_full.py seasons
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/export_full.py")

CONF = {
    # East
    "ATL": "East", "BOS": "East", "BKN": "East", "NJN": "East",
    "CHA": "East", "CHO": "East", "CHH": "East", "CHI": "East",
    "CLE": "East", "DET": "East", "IND": "East", "MIA": "East",
    "MIL": "East", "NYK": "East", "ORL": "East", "PHI": "East",
    "TOR": "East", "WAS": "East", "WSB": "East",
    # West
    "DAL": "West", "DEN": "West", "GSW": "West", "HOU": "West",
    "LAC": "West", "SDC": "West", "LAL": "West", "MEM": "West",
    "VAN": "West", "MIN": "West", "NOP": "West", "NOH": "West",
    "NOK": "West", "OKC": "West", "SEA": "West", "PHX": "West",
    "PHO": "West", "POR": "West", "SAC": "West", "KCK": "West",
    "SAS": "West", "UTA": "West",
}


def conf_case():
    whens = "\n".join("             WHEN '%s' THEN '%s'" % (k, v)
                      for k, v in sorted(CONF.items()))
    return "CASE o.team_abbr\n%s\n             ELSE NULL END AS conference" % whens


ADDITION = '''),
wl AS (
  -- one row per team per game, so a win is just a score comparison
  SELECT season, league, season_type, team_abbr,
         SUM(CASE WHEN won THEN 1 ELSE 0 END) AS w,
         SUM(CASE WHEN won THEN 0 ELSE 1 END) AS l
  FROM (
    SELECT season, league, season_type, home_abbr AS team_abbr,
           home_score > away_score AS won
    FROM all_games
    WHERE season = '%s' AND home_score IS NOT NULL AND home_abbr IS NOT NULL
    UNION ALL
    SELECT season, league, season_type, away_abbr AS team_abbr,
           away_score > home_score AS won
    FROM all_games
    WHERE season = '%s' AND away_score IS NOT NULL AND away_abbr IS NOT NULL
  )
  GROUP BY 1,2,3,4
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("  %s not found" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "conference" in src:
        print("  already patched")
        return
    if "opp_poss_raw" not in src:
        sys.exit("  run patch_team_export.py first")

    # splice the wl CTE in after the opp CTE closes
    anchor = ")\nSELECT o.*,"
    if anchor not in src:
        sys.exit("  could not find the end of the opp CTE")
    src = src.replace(anchor, ADDITION + ")\nSELECT o.*,", 1)

    # add the new output columns
    old_tail = """       ((o.fga + 0.44*o.fta - o.orb + o.tov)
        + (p.opp_fga + 0.44*p.opp_fta - p.opp_orb + p.opp_tov)) / 2.0 AS poss_est
FROM own o
JOIN opp p
  ON p.season = o.season AND p.league = o.league
 AND p.season_type = o.season_type AND p.team_abbr = o.team_abbr
\""""
    new_tail = """       ((o.fga + 0.44*o.fta - o.orb + o.tov)
        + (p.opp_fga + 0.44*p.opp_fta - p.opp_orb + p.opp_tov)) / 2.0 AS poss_est,
       w.w AS wins, w.l AS losses,
       CAST(w.w AS DOUBLE) / NULLIF(w.w + w.l, 0) AS win_pct,
       o.pts - p.opp_pts AS point_diff,
       (o.pts - p.opp_pts) / NULLIF(CAST(o.gp AS DOUBLE), 0) AS mov,
       %s
FROM own o
JOIN opp p
  ON p.season = o.season AND p.league = o.league
 AND p.season_type = o.season_type AND p.team_abbr = o.team_abbr
LEFT JOIN wl w
  ON w.season = o.season AND w.league = o.league
 AND w.season_type = o.season_type AND w.team_abbr = o.team_abbr
\"""" % conf_case()

    if old_tail not in src:
        sys.exit("  TEAM_SQL tail does not match expected form")
    src = src.replace(old_tail, new_tail)

    # the query now interpolates the season four times
    src = src.replace("TEAM_SQL % (s, s),", "TEAM_SQL % (s, s, s, s),")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("  patch invalid (%s) — nothing changed" % e)

    open(TARGET + ".bak_standings", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  team_season now carries:")
    print("    wins, losses, win_pct")
    print("    point_diff, mov      (margin of victory — the real tiebreaker)")
    print("    conference           (East/West, historical abbrs included)")
    print("  backup -> export_full.py.bak_standings")
    print("\n  next: python3 export_full.py seasons")


if __name__ == "__main__":
    main()
