#!/usr/bin/env python3
"""
migrate_to_supabase.py — move the 46 scouting reports into the database.

They were parked in R2 as Parquet while Supabase was down. Now that the editor
writes to Supabase, the app reads from there, and the old reports would be
invisible until they join them.

Three differences between the shapes:

    scout_token   the old records carry adam_dev, gregory_dev and so on; the
                  scouts table keys on adam, gregory
    tier          the old vocabulary says "secondary", the schema says
                  "ancillary" — same meaning, same 0.5 weight
    HELP values   already converted to decimals during the first migration, and
                  the old integer ranks sit inside the new bands exactly, so
                  nothing is approximated here

Writes happen as you, authenticated, which is why the row-level policy lets a
super_admin file under another scout's name. A plain scout running this would
be rejected on every row that is not theirs — which is the policy working.

    python3 migrate_to_supabase.py check     # what would be written
    python3 migrate_to_supabase.py migrate
"""

import argparse
import getpass
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HOME = os.path.expanduser("~/boxandone")
ENV_PATH = os.path.join(HOME, ".env")
PREFIX = "v2/scouting"

SCOUT_MAP = {
    "adam_dev": "adam", "gregory_dev": "gregory",
    "chris_dev": "chris", "jeffsmith": "jeff", "jeff_dev": "jeff",
}
NAME_MAP = {
    "adam": "adam", "adam spinella": "adam",
    "gregory parker-thompson": "gregory", "gregory": "gregory",
    "chris jones": "chris", "chris": "chris",
    "jeff smith": "jeff", "jeff": "jeff",
}
TIER_MAP = {"primary": "primary", "secondary": "ancillary",
            "ancillary": "ancillary", "potential": "potential"}
TIER_WEIGHT = {"primary": 1.0, "ancillary": 0.5, "potential": 0.25}


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


def sign_in(env, email, password):
    url = env["SUPABASE_URL"].rstrip("/") + "/auth/v1/token?grant_type=password"
    body = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "apikey": env["SUPABASE_ANON_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def rest(env, token, path, method="GET", payload=None, prefer=None):
    url = "%s/rest/v1/%s" % (env["SUPABASE_URL"].rstrip("/"), path)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"apikey": env["SUPABASE_ANON_KEY"],
               "Authorization": "Bearer %s" % token,
               "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def load_parquet(env):
    import boto3, duckdb
    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    con = duckdb.connect()
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("reports", "subjects", "help", "archetypes"):
            p = os.path.join(tmp, name + ".parquet")
            s3.download_file(env["R2_BUCKET_NAME"], "%s/%s.parquet" % (PREFIX, name), p)
            cur = con.execute("SELECT * FROM read_parquet('%s')" % p)
            cols = [d[0] for d in cur.description]
            out[name] = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    return out


def scout_of(rep):
    tok = (rep.get("scout_token") or "").strip().lower()
    if tok in SCOUT_MAP:
        return SCOUT_MAP[tok]
    name = (rep.get("scout_name") or "").strip().lower()
    if name in NAME_MAP:
        return NAME_MAP[name]
    for key, sid in SCOUT_MAP.items():
        if tok.startswith(key.split("_")[0]):
            return sid
    return None


def build(data):
    subj = {}
    for s in data["subjects"]:
        subj.setdefault(s["report_id"], []).append(s)
    helps = {h["report_id"]: h for h in data["help"]}
    arch = {}
    for a in data["archetypes"]:
        arch.setdefault(a["report_id"], []).append(a)

    reports, assessments, archrows, problems = [], [], [], []
    for r in data["reports"]:
        rid = r["report_id"]
        sid = scout_of(r)
        subjects = subj.get(rid, [])
        if not sid:
            problems.append((rid, "no scout for %r / %r"
                             % (r.get("scout_token"), r.get("scout_name"))))
            continue
        if not subjects:
            problems.append((rid, "no subject"))
            continue
        pid = subjects[0]["player_id"]
        date = str(r.get("created_at") or "")[:10] or None
        if not date:
            problems.append((rid, "no date"))
            continue

        h = helps.get(rid)
        reports.append({
            "report_id": rid, "player_id": pid, "scout_id": sid,
            # the check constraint lists lowercase types: help, game, film,
            # workout, interview, medical, character, quick_note, trade_target
            "report_type": (r.get("report_type") or "help").lower(),
            "report_date": date,
            "contemporaneous": True,
            "league": "NBA",
            "season": str(r["season_year"]) if r.get("season_year") else None,
            "body": r.get("body") or None,
            "status": r.get("status") or "published",
            # reports allow human | model | imported — and this is an import,
            # which is a more honest record than claiming it was typed in here
            "provenance": "imported",
        })
        # the database enforces low <= expected <= high, so a record that
        # breaks it is reported rather than silently reordered
        if h:
            lo, ex, hi = h.get("low_val"), h.get("exp_val"), h.get("high_val")
            if None not in (lo, ex, hi) and not (lo <= ex <= hi):
                problems.append((rid, "HELP out of order: %s/%s/%s" % (lo, ex, hi)))
                continue
        if h:
            assessments.append({
                "report_id": rid, "player_id": pid, "scout_id": sid,
                "report_date": date,
                "high_val": h.get("high_val"),
                "expected_val": h.get("exp_val"),
                "low_val": h.get("low_val"),
                "position_at_report": h.get("position"),
            })
        pos = (h or {}).get("position")
        if not pos and arch.get(rid):
            # player_archetypes has a composite key into archetypes on
            # (position, archetype), so defaulting the position would either
            # violate the key or quietly file the archetype under the wrong
            # one. Better to drop the row and say so.
            problems.append((rid, "archetypes present but no position — skipped"))
        for a in (arch.get(rid, []) if pos else []):
            tier = TIER_MAP.get((a.get("tier") or "").lower(), "ancillary")
            # weight is a generated column — the database computes it from
            # tier, so sending a value is rejected outright. That is the better
            # design: the weight cannot drift from the tier it represents.
            archrows.append({
                "player_id": pid, "position": pos,
                "archetype": a["archetype"], "tier": tier,
                "effective_from": date, "assigned_by": sid, "report_id": rid,
            })
    return reports, assessments, archrows, problems


def cmd_check(env):
    data = load_parquet(env)
    reports, assessments, archrows, problems = build(data)
    print("  %d reports -> %d ready, %d with problems"
          % (len(data["reports"]), len(reports), len(problems)))
    print("  %d help assessments, %d archetype rows" % (len(assessments), len(archrows)))
    by = {}
    for r in reports:
        by[r["scout_id"]] = by.get(r["scout_id"], 0) + 1
    print("\n  by scout: %s" % by)
    dates = sorted(r["report_date"] for r in reports)
    print("  dates: %s .. %s" % (dates[0], dates[-1]))
    tiers = {}
    for a in archrows:
        tiers[a["tier"]] = tiers.get(a["tier"], 0) + 1
    print("  archetype tiers: %s" % tiers)
    if problems:
        print("\n  problems:")
        for rid, why in problems[:10]:
            print("    %s  %s" % (rid[:12], why))
    print("\n  sample:")
    for r in reports[:3]:
        h = next((a for a in assessments if a["report_id"] == r["report_id"]), {})
        print("    %s  %-8s %s  H%.0f E%.0f L%.0f  %d chars"
              % (r["report_date"], r["scout_id"], r["player_id"],
                 h.get("high_val") or 0, h.get("expected_val") or 0,
                 h.get("low_val") or 0, len(r["body"] or "")))


def cmd_migrate(env):
    email = input("  email [spinella14@gmail.com]: ").strip() or "spinella14@gmail.com"
    token = sign_in(env, email, getpass.getpass("  password: "))
    print("  signed in\n")

    # the players table must hold these ids, or every insert fails the key
    code, who = rest(env, token, "scouts?select=scout_id,role")
    print("  scouts visible: %s" % [s["scout_id"] for s in (who or [])])
    code, n = rest(env, token, "players?select=player_id&limit=1")
    if code == 200 and not n:
        print("\n  WARNING: the players table is empty. If reports.player_id")
        print("  references it, every insert will fail. Seed players first.")
        if input("  continue anyway? [y/N] ").strip().lower() != "y":
            return

    data = load_parquet(env)
    reports, assessments, archrows, problems = build(data)

    for label, path, rows in (("reports", "reports", reports),
                              ("help_assessments", "help_assessments", assessments),
                              ("player_archetypes", "player_archetypes", archrows)):
        ok = 0
        for i in range(0, len(rows), 25):
            batch = rows[i:i + 25]
            code, body = rest(env, token, path, "POST", batch,
                              prefer="resolution=merge-duplicates,return=minimal")
            if code in (200, 201, 204):
                ok += len(batch)
            else:
                print("  %s: HTTP %s on rows %d-%d" % (label, code, i, i + len(batch)))
                print("     %s" % str(body)[:220])
                break
        print("  %-18s %d/%d written" % (label, ok, len(rows)))

    code, cnt = rest(env, token, "reports?select=report_id")
    print("\n  reports now in the database: %s" % (len(cnt) if isinstance(cnt, list) else cnt))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "migrate"])
    a = ap.parse_args()
    e = load_env()
    (cmd_check if a.cmd == "check" else cmd_migrate)(e)
