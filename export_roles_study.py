#!/usr/bin/env python3
"""
export_roles_study.py — one table for the first archetype study. Reads only.

For every player with at least 250 regular-season minutes in a season, one row:
his season-role labels from the Season roles tool (position, primary,
ancillary, potential), joined to that season's profile:

  box and shot diet    minutes, games, per-36 lines, TS%, the eleven shot zones
                       (attempts and makes), turnover types
  assisted shots       how many of his makes in each zone were assisted
  play types           Synergy possessions and points per type, offense and defense
  tracking             passing, touches, time of possession, shots by dribbles
                       (whatever ingest_tracking.py holds for the season)

2026-27 labels are joined to 2025-26's profile too ("stats_season"), since two
games of this season say nothing yet; that is also what shows how a role you
gave in 2026-27 differs from the one you gave for 2025-26.

Writes ~/boxandone/data/roles_study.csv. Nothing is changed anywhere.

    python3 export_roles_study.py
"""
import csv
import os
import sys
import tempfile

HOME = os.path.expanduser("~/boxandone")
sys.path.insert(0, HOME)
import nightly2 as N              # noqa: E402
import sync_players_supabase as SP  # noqa: E402

OUT = os.path.join(HOME, "data", "roles_study.csv")
LABEL_SEASONS = {"2025-26": "2025-26", "2026-27": "2025-26",
                 "2024-25": "2024-25", "2023-24": "2023-24"}
MIN_MP = 250


def supabase_rows(env, path):
    out = []
    for off in range(0, 100000, 1000):
        code, page = SP.call(env, None, "%s&limit=1000&offset=%d" % (path, off))
        if code != 200:
            sys.exit("  Supabase said %s for %s: %s" % (code, path.split("?")[0], str(page)[:200]))
        out += page
        if len(page) < 1000:
            return out
    return out


def main():
    import duckdb
    env = N.load_env()
    if not env.get("SUPABASE_SERVICE_ROLE_KEY"):
        sys.exit("  SUPABASE_SERVICE_ROLE_KEY missing from ~/boxandone/.env")
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]

    pos = supabase_rows(env, "season_positions?select=player_id,season,position&order=player_id")
    arch = supabase_rows(env, "season_archetypes?select=player_id,season,archetype,tier&order=player_id")
    labels = {}
    for r in pos:
        labels[(r["season"], r["player_id"])] = {"position": r["position"], "primary": "",
                                                 "ancillary": [], "potential": []}
    for r in arch:
        L = labels.get((r["season"], r["player_id"]))
        if not L:
            continue
        if r["tier"] == "primary":
            L["primary"] = r["archetype"]
        else:
            L[r["tier"]].append(r["archetype"])
    have = sorted({s for s, _ in labels})
    print("  labels: %s" % ", ".join("%s %d (%d with a primary)" % (
        s, sum(1 for (x, _) in labels if x == s),
        sum(1 for (x, _), L in labels.items() if x == s and L["primary"])) for s in have))

    con = duckdb.connect()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        for s in sorted(set(LABEL_SEASONS.values())):
            for name in ("player_season", "assisted", "playtypes", "tracking", "defense"):
                p = os.path.join(tmp, "%s_%s.parquet" % (name, s))
                try:
                    s3.download_file(bucket, "v2/seasons/%s/%s.parquet" % (s, name), p)
                    files[(s, name)] = p
                except Exception:
                    pass
        for label_season in [s for s in LABEL_SEASONS if s in have]:
            stats = LABEL_SEASONS[label_season]
            if (stats, "player_season") not in files:
                print("  %s: no season file — skipped" % stats)
                continue
            q = "SELECT b.* FROM read_parquet('%s') b" % files[(stats, "player_season")]
            joins = []
            for name, pre in (("assisted", "ast"), ("playtypes", None), ("tracking", "trk"),
                              ("defense", None)):
                if (stats, name) not in files:
                    continue
                p = files[(stats, name)]
                cols = [c[0] for c in con.execute("SELECT * FROM read_parquet('%s') LIMIT 0" % p).description]
                keep = [c for c in cols if c not in ("player_id", "season", "season_type", "nba_person_id",
                                                     "player_name", "full_name")]
                alias = "j_%s" % name
                sel = ", ".join('%s."%s" AS "%s"' % (alias, c, (pre + "_" + c) if pre else c) for c in keep)
                joins.append((alias, p, sel, "player_id" in cols, "season_type" in cols))
            joins = [j for j in joins if j[3] and j[2]]      # a file without player ids cannot join
            selects = ["b.*"] + [j[2] for j in joins]
            sql = "SELECT %s FROM read_parquet('%s') b" % (", ".join(selects), files[(stats, "player_season")])
            for alias, p, _, has_pid, has_st in joins:
                if not has_pid:
                    continue
                cond = "%s.player_id = b.player_id" % alias
                if has_st:
                    cond += " AND %s.season_type = b.season_type" % alias
                sql += " LEFT JOIN read_parquet('%s') %s ON %s" % (p, alias, cond)
            sql += " WHERE b.season_type = 'regular' AND b.mp >= %d" % MIN_MP
            res = con.execute(sql)
            names = [d[0] for d in res.description]
            for r in res.fetchall():
                d = dict(zip(names, r))
                L = labels.get((label_season, d["player_id"]))
                if label_season != stats and not L:
                    continue          # 2026-27: only the players you have labelled
                d = {"label_season": label_season, "stats_season": stats,
                     "lab_position": (L or {}).get("position", ""),
                     "lab_primary": (L or {}).get("primary", ""),
                     "lab_ancillary": "|".join((L or {}).get("ancillary", [])),
                     "lab_potential": "|".join((L or {}).get("potential", [])), **d}
                rows.append(d)
    if not rows:
        sys.exit("  nothing to write")
    cols = list(dict.fromkeys(c for r in rows for c in r))
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})
    print("  wrote %d rows, %d columns -> %s" % (len(rows), len(cols), OUT))
    print("  attach that file in the chat")


if __name__ == "__main__":
    main()
