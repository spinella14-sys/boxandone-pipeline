#!/usr/bin/env python3
"""
patch_rapm_six.py — run once. The RAPM split moves from the four factors to
the six (decided 2026-11-01): true shooting, turnovers and offensive
rebounding, on each end.

  ts    TS%    PTS / (2 × (FGA + 0.44 × FTA))     weighted by true-shot attempts
  tov   TOV%   TOV / possessions                  weighted by possessions
  orb   OREB%  OREB / (OREB + opponent DREB)      weighted by those chances

Each player gets o_/d_ts, tov, orb in the factor's own units (percentage
points) and o_/d_..._pts in points per 100, from what each factor is worth
that season (fitted on team-games with ts, tov and orb only). Free-throw rate
is still fitted, as o_ft / d_ft in its own units, a diagnostic with no
points value: on 15,562 team-games it added nothing once TS% was in.

Rebuild afterwards:
    python3 compute_rapm.py check --season 2024-25
    python3 compute_rapm.py build --seasons 2019-20:2026-27
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "compute_rapm.py")
src = open(PATH).read()
if '("ts", lambda' in src:
    print("  compute_rapm.py already patched")
    sys.exit()

KEYS = '("ts", "tov", "orb")'
PAIRS = [
    ('NEED = ("fgm", "fga", "fg3m", "ftm", "orb", "drb", "tov")',
     'NEED = ("fgm", "fga", "fg3m", "fta", "ftm", "orb", "drb", "tov")'),
    ('''# name, rate (offence's tallies, defence's tallies, possessions) -> (value, weight),
# and the sign that makes "more of this" good for the offence
FACTOR_DEFS = (
    ("efg", lambda o, d, q: ((o["fgm"] + 0.5 * o["fg3m"]) / o["fga"], o["fga"]) if o["fga"] else (0.0, 0.0), 1),
    ("tov", lambda o, d, q: (o["tov"] / q, q) if q else (0.0, 0.0), -1),
    ("orb", lambda o, d, q: (o["orb"] / (o["orb"] + d["drb"]), o["orb"] + d["drb"])
                            if o["orb"] + d["drb"] else (0.0, 0.0), 1),
    ("ft", lambda o, d, q: (o["ftm"] / o["fga"], o["fga"]) if o["fga"] else (0.0, 0.0), 1),
)''',
     '''# the six factors (three a side): name, rate (offence's tallies, defence's
# tallies, possessions, points) -> (value, weight), and the sign that makes
# "more of this" good for the offence. ft is a diagnostic: fitted and shown in
# its own units, but it carries no points value (it added nothing beside TS%).
def _tsa(o):
    return o["fga"] + 0.44 * o["fta"]


FACTOR_DEFS = (
    ("ts", lambda o, d, q, p: (p / (2 * _tsa(o)), _tsa(o)) if _tsa(o) else (0.0, 0.0), 1),
    ("tov", lambda o, d, q, p: (o["tov"] / q, q) if q else (0.0, 0.0), -1),
    ("orb", lambda o, d, q, p: (o["orb"] / (o["orb"] + d["drb"]), o["orb"] + d["drb"])
                               if o["orb"] + d["drb"] else (0.0, 0.0), 1),
    ("ft", lambda o, d, q, p: (o["ftm"] / o["fga"], o["fga"]) if o["fga"] else (0.0, 0.0), 1),
)
CORE = ("ts", "tov", "orb")'''),
    ('''        vals = [f(t[0], t[1], ob[4]) if t else (0.0, 0.0) for ob, t in zip(obs, tal)]''',
     '''        vals = [f(t[0], t[1], ob[4], ob[3]) if t else (0.0, 0.0) for ob, t in zip(obs, tal)]'''),
    ('''        a = agg.setdefault(k, {"pts": 0, "poss": 0, "fgm": 0, "fga": 0, "fg3m": 0, "ftm": 0,
                               "orb": 0, "opp_drb": 0, "tov": 0})
        a["pts"] += ob[3]
        a["poss"] += ob[4]
        for x in ("fgm", "fga", "fg3m", "ftm", "orb", "tov"):''',
     '''        a = agg.setdefault(k, {"pts": 0, "poss": 0, "fgm": 0, "fga": 0, "fg3m": 0, "ftm": 0,
                               "fta": 0, "orb": 0, "opp_drb": 0, "tov": 0})
        a["pts"] += ob[3]
        a["poss"] += ob[4]
        for x in ("fgm", "fga", "fg3m", "ftm", "fta", "orb", "tov"):'''),
    ('''        X.append([1.0, (a["fgm"] + 0.5 * a["fg3m"]) / a["fga"], a["tov"] / a["poss"],
                  a["orb"] / (a["orb"] + a["opp_drb"]), a["ftm"] / a["fga"]])''',
     '''        X.append([1.0, a["pts"] / (2 * (a["fga"] + 0.44 * a["fta"])), a["tov"] / a["poss"],
                  a["orb"] / (a["orb"] + a["opp_drb"])])'''),
    ('''    return {"efg": float(beta[1]), "tov": float(beta[2]), "orb": float(beta[3]),
            "ft": float(beta[4]), "r2": float(r2)}''',
     '''    return {"ts": float(beta[1]), "tov": float(beta[2]), "orb": float(beta[3]), "r2": float(r2)}'''),
    ('''            row["o_%s_pts" % name] = round(worth[name] * float(fo), 3)
            row["d_%s_pts" % name] = round(-worth[name] * float(fd), 3)''',
     '''            if name in worth:                   # ft is a diagnostic: no points value
                row["o_%s_pts" % name] = round(worth[name] * float(fo), 3)
                row["d_%s_pts" % name] = round(-worth[name] * float(fd), 3)'''),
    ('''            print("            factors: lambda %s · worth per 100 poss: eFG %+.0f, TOV %+.0f, "
                  "OREB %+.0f, FT %+.0f (R² %.2f)"
                  % ("/".join(str(m["factors"][k]["lambda"]) for k in ("efg", "tov", "orb", "ft")),
                     wv["efg"], wv["tov"], wv["orb"], wv["ft"], wv["r2"]))''',
     '''            print("            six factors: lambda %s (ft %s) · worth per 100 poss: TS %+.0f, TOV %+.0f, "
                  "OREB %+.0f (R² %.2f)"
                  % ("/".join(str(m["factors"][k]["lambda"]) for k in CORE), m["factors"]["ft"]["lambda"],
                     wv["ts"], wv["tov"], wv["orb"], wv["r2"]))'''),
    ('''    fx = pool and "o_efg_pts" in pool[0]''', '''    fx = pool and "o_ts_pts" in pool[0]'''),
    ('''              "offence: eFG  TOV  OREB  FT   |  defence: eFG  TOV  OREB  FT   (points per 100)"))''',
     '''              "offence: TS   TOV  OREB  |  defence: TS   TOV  OREB   (points per 100)"))'''),
    ('''            line += "   " + " ".join("%+5.1f" % r["o_%s_pts" % k] for k in ("efg", "tov", "orb", "ft"))
            line += "  | " + " ".join("%+5.1f" % r["d_%s_pts" % k] for k in ("efg", "tov", "orb", "ft"))''',
     '''            line += "   " + " ".join("%+5.1f" % r["o_%s_pts" % k] for k in CORE)
            line += "  | " + " ".join("%+5.1f" % r["d_%s_pts" % k] for k in CORE)'''),
    ('''        so = np.array([sum(r["o_%s_pts" % k] for k in ("efg", "tov", "orb", "ft")) for r in pool])
        sd = np.array([sum(r["d_%s_pts" % k] for k in ("efg", "tov", "orb", "ft")) for r in pool])''',
     '''        so = np.array([sum(r["o_%s_pts" % k] for k in CORE) for r in pool])
        sd = np.array([sum(r["d_%s_pts" % k] for k in CORE) for r in pool])'''),
]

for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  compute_rapm.py not as expected (%r) — nothing written" % old[:70])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  compute_rapm.py patched: six factors (TS%, TOV%, OREB% each end), FT rate as a diagnostic")
