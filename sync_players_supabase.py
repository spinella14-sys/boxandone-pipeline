#!/usr/bin/env python3
"""
sync_players_supabase.py — copy players Supabase does not have yet.

Scouting writes (reports, game-note tags, HELP, archetypes) reference
Supabase's players table, which was seeded once from a 3,849-player registry.
Every rookie and signing added since exists in our registry and the app's
search, but not there, so tagging one fails with a foreign-key error.

This inserts only the missing players. Existing Supabase rows are never
touched, so nothing an admin has edited there can be overwritten.

Credentials, in order:
  SUPABASE_SERVICE_ROLE_KEY in ~/boxandone/.env   no prompt — what the hourly
                                                  and nightly jobs use
  otherwise                                       signs in as you (asks for
                                                  your password)

    python3 sync_players_supabase.py           # insert what is missing
    python3 sync_players_supabase.py --check   # count only, write nothing
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
import seed_players_supabase as S  # noqa: E402  (registry reader, constraints)


def headers(env, token):
    key = env.get("SUPABASE_SERVICE_ROLE_KEY")
    if key:
        h = {"apikey": key}
        # legacy service_role keys are JWTs and go in Authorization too;
        # the newer sb_secret_ keys go in apikey only
        if key.startswith("eyJ"):
            h["Authorization"] = "Bearer %s" % key
        return h
    return {"apikey": env["SUPABASE_ANON_KEY"], "Authorization": "Bearer %s" % token}


def call(env, token, path, method="GET", payload=None, prefer=None):
    url = "%s/rest/v1/%s" % (env["SUPABASE_URL"].rstrip("/"), path)
    h = dict(headers(env, token), **{"Content-Type": "application/json"})
    if prefer:
        h["Prefer"] = prefer
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def supabase_ids(env, token):
    got, offset = set(), 0
    while True:
        code, rows = call(env, token, "players?select=player_id&order=player_id"
                                      "&limit=1000&offset=%d" % offset)
        if code != 200:
            sys.exit("  could not read Supabase players: HTTP %s %s" % (code, rows))
        got.update(r["player_id"] for r in rows)
        if len(rows) < 1000:
            return got
        offset += 1000


def main():
    check = "--check" in sys.argv
    env = S.load_env()
    token = None
    if not env.get("SUPABASE_SERVICE_ROLE_KEY"):
        # only prompt when a person is at the terminal — under the nightly
        # job output is captured, and a hidden password prompt would hang it
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("  no SUPABASE_SERVICE_ROLE_KEY — skipping player sync")
            return
        email = input("  email [spinella14@gmail.com]: ").strip() or "spinella14@gmail.com"
        token = S.sign_in(env, email, getpass.getpass("  password: "))

    local = S.read_registry()
    have = supabase_ids(env, token)
    missing = [r for r in local if r["player_id"] not in have]
    print("  registry %d   supabase %d   missing %d"
          % (len(local), len(have), len(missing)))
    if check or not missing:
        return
    for r in missing:
        # these came from NBA.com rosters, not the Basketball Reference scrape,
        # but provenance only distinguishes scraped / human / model / derived
        r["provenance"] = "scraped"
    done = 0
    for i in range(0, len(missing), S.BATCH):
        batch = missing[i:i + S.BATCH]
        code, body = call(env, token, "players", "POST", batch,
                          prefer="resolution=ignore-duplicates,return=minimal")
        if code not in (200, 201, 204):
            sys.exit("  HTTP %s inserting rows %d-%d: %s" % (code, i, i + len(batch), body))
        done += len(batch)
    print("  inserted %d players" % done)
    for r in missing[:8]:
        print("    %s  %s" % (r["player_id"], r["full_name"]))


if __name__ == "__main__":
    main()
