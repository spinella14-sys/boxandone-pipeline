#!/usr/bin/env python3
"""
migrate_reports.py — bring the 46 existing scouting reports into the new shape.

They are the only irreplaceable data in the project: stats can be re-scraped,
written evaluations cannot. They currently sit as loose JSON under
scouting/reports/{nba_id}/{uuid}.json in the old layout.

Three conversions:

  player id   stored as the NBA person id; the app keys on internal P########,
              so it joins through player_identifiers
  HELP codes  stored as codes, and in the OLD vocabulary — TS and TW became KS
              and BO. Conveniently the old integer ranks land inside the new
              bands exactly (old RE was rank 5; new RE is [4.75, 5.75)), so the
              decimal is the rank itself rather than a band midpoint. Nothing
              is invented.
  archetypes  "Shooter:primary,Defender:secondary" into rows

The output schema carries report_type from the start, because HELP reports are
one of four kinds planned — Game Notes, ARCH and Event Notes to follow — and
retrofitting a type column later means rewriting every consumer.

Game Notes in particular tag several players in one report, so subjects are a
separate table rather than a column: one report, many player rows, each with
the excerpt that mentions them.

    python3 migrate_reports.py plan      # parse and report, write nothing
    python3 migrate_reports.py migrate   # write parquet to R2

Writes v2/scouting/reports.parquet, subjects.parquet, help.parquet,
archetypes.parquet.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
PREFIX = "v2/scouting"

RENAME = {"TS": "KS", "TW": "BO"}
RANK = {"FR": 10, "CS": 9, "KS": 8, "ST": 7, "KR": 6,
        "RE": 5, "RO": 4, "BO": 3, "GL": 2, "ML": 1}


def load_env():
    if not os.path.exists(ENV_PATH):
        sys.exit("  %s not found" % ENV_PATH)
    env = {}
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3c(env):
    import boto3
    return boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                        region_name="auto")


def newest_backup():
    root = os.path.join(HOME, "backups", "r2")
    if not os.path.isdir(root):
        sys.exit("  no backup found at %s — run backup_r2.py first" % root)
    runs = sorted(os.listdir(root))
    if not runs:
        sys.exit("  no backup runs found")
    return os.path.join(root, runs[-1], "scouting", "reports")


def nba_to_internal():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    m = {r[0]: r[1] for r in con.execute(
        "SELECT source_id, player_id FROM player_identifiers WHERE source='nba'").fetchall()}
    names = {r[0]: r[1] for r in con.execute(
        "SELECT player_id, full_name FROM players").fetchall()}
    con.close()
    return m, names


def help_decimal(code):
    """Old code -> (new code, decimal). The old integer rank sits inside the
    new band, so it is the value, not an approximation of one."""
    if not code:
        return None, None
    c = RENAME.get(code.strip().upper(), code.strip().upper())
    return (c, float(RANK[c])) if c in RANK else (None, None)


def parse_archetypes(s):
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, tier = part.split(":", 1)
        else:
            name, tier = part, "primary"
        out.append((name.strip(), tier.strip().lower()))
    return out


def load_all():
    base = newest_backup()
    files = sorted(glob.glob(os.path.join(base, "**", "*.json"), recursive=True))
    out = []
    for f in files:
        try:
            out.append(json.load(open(f, encoding="utf-8")))
        except Exception as e:
            print("    unreadable: %s (%s)" % (os.path.basename(f), e))
    return out


def build(raw, idmap, names):
    reports, subjects, helps, archs = [], [], [], []
    unmapped = []
    for d in raw:
        rid = d.get("report_id") or d.get("recordId")
        nba = str(d.get("player_id") or "")
        pid = idmap.get(nba)
        if not pid:
            unmapped.append((nba, d.get("player_name")))
            continue

        created = (d.get("created_at") or "")[:10] or None
        reports.append({
            "report_id": rid,
            "report_type": "HELP",
            "scout_token": d.get("author_token") or "",
            "scout_name": d.get("author_name") or "",
            "created_at": created,
            "updated_at": (d.get("updated_at") or "")[:10] or None,
            "season_year": d.get("season_year"),
            "status": d.get("status") or "published",
            "game_id": None,
            "team_abbr": None,
            "title": None,
            "body": d.get("notes") or d.get("body") or "",
        })
        subjects.append({
            "report_id": rid, "player_id": pid,
            "player_name": names.get(pid) or d.get("player_name") or "",
            "excerpt": None, "ordinal": 0,
        })

        hi_c, hi_v = help_decimal(d.get("help_high"))
        ex_c, ex_v = help_decimal(d.get("help_expected"))
        lo_c, lo_v = help_decimal(d.get("help_low"))
        if hi_c or ex_c or lo_c:
            helps.append({
                "report_id": rid, "player_id": pid,
                "high_code": hi_c, "high_val": hi_v,
                "exp_code": ex_c, "exp_val": ex_v,
                "low_code": lo_c, "low_val": lo_v,
                "position": d.get("pos_col") or None,
                "created_at": created,
            })
        for i, (name, tier) in enumerate(parse_archetypes(d.get("archetypes"))):
            archs.append({"report_id": rid, "player_id": pid,
                          "archetype": name, "tier": tier, "ordinal": i})
    return reports, subjects, helps, archs, unmapped


def cmd_plan(env):
    raw = load_all()
    idmap, names = nba_to_internal()
    reports, subjects, helps, archs, unmapped = build(raw, idmap, names)

    print("  %d json files -> %d reports" % (len(raw), len(reports)))
    print("  %d subjects, %d HELP assessments, %d archetype rows"
          % (len(subjects), len(helps), len(archs)))
    if unmapped:
        print("\n  UNMAPPED players (no nba id in the registry):")
        for nba, nm in unmapped:
            print("    %-10s %s" % (nba, nm))

    by_scout = {}
    for r in reports:
        by_scout[r["scout_name"]] = by_scout.get(r["scout_name"], 0) + 1
    print("\n  by scout:", by_scout)

    old_codes = set()
    for d in raw:
        for k in ("help_high", "help_expected", "help_low"):
            if d.get(k):
                old_codes.add(d[k])
    print("  codes in source:", ", ".join(sorted(old_codes)))
    renamed = sorted(c for c in old_codes if c in RENAME)
    if renamed:
        print("  renamed: " + ", ".join("%s->%s" % (c, RENAME[c]) for c in renamed))

    print("\n  sample:")
    for h in helps[:5]:
        nm = names.get(h["player_id"], "?")
        print("    %-22s %s %.0f / %s %.0f / %s %.0f   pos=%s"
              % (nm[:22], h["high_code"], h["high_val"], h["exp_code"], h["exp_val"],
                 h["low_code"], h["low_val"], h["position"]))
    dates = sorted(r["created_at"] for r in reports if r["created_at"])
    if dates:
        print("\n  published %s .. %s" % (dates[0], dates[-1]))


def cmd_migrate(env):
    import duckdb, tempfile
    raw = load_all()
    idmap, names = nba_to_internal()
    reports, subjects, helps, archs, unmapped = build(raw, idmap, names)
    if unmapped:
        print("  %d reports skipped for unmapped players" % len(unmapped))
    if not reports:
        sys.exit("  nothing to write")

    s3 = s3c(env)
    con = duckdb.connect()
    tables = {
        "reports": (reports, """report_id VARCHAR, report_type VARCHAR,
            scout_token VARCHAR, scout_name VARCHAR, created_at DATE,
            updated_at DATE, season_year INTEGER, status VARCHAR,
            game_id VARCHAR, team_abbr VARCHAR, title VARCHAR, body VARCHAR"""),
        "subjects": (subjects, """report_id VARCHAR, player_id VARCHAR,
            player_name VARCHAR, excerpt VARCHAR, ordinal INTEGER"""),
        "help": (helps, """report_id VARCHAR, player_id VARCHAR,
            high_code VARCHAR, high_val DOUBLE, exp_code VARCHAR, exp_val DOUBLE,
            low_code VARCHAR, low_val DOUBLE, position VARCHAR, created_at DATE"""),
        "archetypes": (archs, """report_id VARCHAR, player_id VARCHAR,
            archetype VARCHAR, tier VARCHAR, ordinal INTEGER"""),
    }
    for name, (rows, ddl) in tables.items():
        con.execute("CREATE OR REPLACE TABLE t (%s)" % ddl)
        if rows:
            cols = [c.strip().split()[0] for c in ddl.replace("\n", " ").split(",")]
            con.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * len(cols)),
                            [tuple(r.get(c) for c in cols) for r in rows])
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            p = f.name
        con.execute("COPY t TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % p)
        s3.upload_file(p, env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, name))
        print("  %-12s %4d rows  %5.0f KB" % (name, len(rows), os.path.getsize(p) / 1024))
        os.unlink(p)
    con.close()
    print("\n  written under %s/" % PREFIX)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "migrate"])
    a = ap.parse_args()
    e = load_env()
    (cmd_plan if a.cmd == "plan" else cmd_migrate)(e)
