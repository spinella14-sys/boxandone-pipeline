#!/usr/bin/env python3
"""
seed_players_supabase.py — put the registry into Supabase.

reports.player_id, help_assessments.player_id and player_archetypes.player_id
all reference players, so nothing scouting-related can be written until the
3,849 players exist there. The registry is already the authority on identity;
this copies it across.

Only identity goes over. Position lives in player_positions because it is
temporal, and height and weight are measurements that belong with the anthro
data rather than the name record — so neither is seeded here even though the
local table carries them.

    python3 seed_players_supabase.py check     # what would be written
    python3 seed_players_supabase.py seed
    python3 seed_players_supabase.py verify    # compare both sides
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
ENV_PATH = os.path.join(HOME, ".env")
BATCH = 250

# allowed by the check constraints on the players table
BIRTH_STATUS = {s: s for s in
                ("confirmed", "unconfirmed", "missing", "admin_verified")}
STATUS = {s: s for s in
          ("active", "inactive", "retired", "prospect", "unknown")}


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
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def read_registry():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)
    cur = con.execute("""
        SELECT player_id, full_name, display_name, name_normalized,
               birthdate, birthdate_status, college, draft_year, draft_round,
               draft_pick, status, nationality
        FROM players ORDER BY player_id""")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()

    out = []
    for r in rows:
        name = r["full_name"] or ""
        parts = name.split()
        out.append({
            "player_id": r["player_id"],
            "full_name": name,
            "display_name": r.get("display_name") or name,
            "first_name": parts[0] if parts else None,
            "last_name": " ".join(parts[1:]) if len(parts) > 1 else None,
            "name_normalized": r.get("name_normalized") or name.lower(),
            "birthdate": str(r["birthdate"]) if r.get("birthdate") else None,
            # Three columns carry check constraints, so the values have to be
            # from their lists rather than something sensible-looking:
            #   birthdate_status  confirmed | unconfirmed | missing | admin_verified
            #   status            active | inactive | retired | prospect | unknown
            #   provenance        scraped | human | model | derived
            "birthdate_status": BIRTH_STATUS.get(
                (r.get("birthdate_status") or "").lower(),
                "confirmed" if r.get("birthdate") else "missing"),
            "college": r.get("college"),
            "draft_year": r.get("draft_year"),
            "draft_round": r.get("draft_round"),
            "draft_pick": r.get("draft_pick"),
            "nationality": r.get("nationality"),
            "status": STATUS.get((r.get("status") or "").lower(), "unknown"),
            # these players came out of the Basketball Reference scrape
            "provenance": "scraped",
        })
    return out


def cmd_check(env):
    rows = read_registry()
    print("  %d players in the local registry" % len(rows))
    have = sum(1 for r in rows if r["birthdate"])
    print("  %d with a birthdate, %d with a college"
          % (have, sum(1 for r in rows if r["college"])))
    from collections import Counter
    print("\n  constrained values, as they will be sent:")
    for f, allowed in (("birthdate_status", set(BIRTH_STATUS)),
                       ("status", set(STATUS)),
                       ("provenance", {"scraped", "human", "model", "derived"})):
        c = Counter(r[f] for r in rows)
        bad = {k: v for k, v in c.items() if k not in allowed}
        print("    %-18s %s%s" % (f, dict(c), "   REJECTED: %s" % bad if bad else ""))

    print("\n  required fields, non-null on every row:")
    for f in ("player_id", "full_name", "name_normalized", "birthdate_status",
              "status", "provenance"):
        bad = sum(1 for r in rows if not r.get(f))
        print("    %-18s %s" % (f, "ok" if bad == 0 else "%d MISSING" % bad))
    print("\n  sample:")
    for r in rows[:3]:
        print("    %s  %-24s %s  %s" % (r["player_id"], r["full_name"][:24],
                                        r["birthdate"] or "no dob", r["status"]))
    print("\n  %d batches of %d" % ((len(rows) + BATCH - 1) // BATCH, BATCH))


def cmd_seed(env):
    email = input("  email [spinella14@gmail.com]: ").strip() or "spinella14@gmail.com"
    token = sign_in(env, email, getpass.getpass("  password: "))
    rows = read_registry()
    print("\n  writing %d players" % len(rows))

    ok = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        code, body = rest(env, token, "players", "POST", batch,
                          prefer="resolution=merge-duplicates,return=minimal")
        if code in (200, 201, 204):
            ok += len(batch)
            if (i // BATCH) % 4 == 0:
                print("    %d/%d" % (ok, len(rows)))
        else:
            print("    HTTP %s at rows %d-%d" % (code, i, i + len(batch)))
            print("      %s" % str(body)[:260])
            break
    print("\n  %d/%d written" % (ok, len(rows)))
    if ok == len(rows):
        print("  next: python3 migrate_to_supabase.py migrate")


def cmd_verify(env):
    email = input("  email [spinella14@gmail.com]: ").strip() or "spinella14@gmail.com"
    token = sign_in(env, email, getpass.getpass("  password: "))
    local = read_registry()
    code, body = rest(env, token, "players?select=player_id&limit=1",
                      prefer="count=exact")
    # PostgREST returns the count in a header, which urllib hides here, so
    # fall back to pulling ids in pages
    got, offset = set(), 0
    while True:
        code, rows = rest(env, token,
                          "players?select=player_id&order=player_id&limit=1000&offset=%d" % offset)
        if code != 200 or not rows:
            break
        got.update(r["player_id"] for r in rows)
        if len(rows) < 1000:
            break
        offset += 1000
    print("  local %d   supabase %d" % (len(local), len(got)))
    missing = [r["player_id"] for r in local if r["player_id"] not in got]
    if missing:
        print("  %d missing, first few: %s" % (len(missing), missing[:5]))
    else:
        print("  every registry player is present")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "seed", "verify"])
    a = ap.parse_args()
    e = load_env()
    {"check": cmd_check, "seed": cmd_seed, "verify": cmd_verify}[a.cmd](e)
