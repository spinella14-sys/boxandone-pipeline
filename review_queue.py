#!/usr/bin/env python3
"""
review_queue.py — the people waiting for a human to say who they are, and
carrying out what was decided.

Three kinds of item, each with the candidates it might be and what we know:

  staged   staging_players rows still pending: an NBA.com (or Basketball
           Reference) id whose name we could not tie to one player — usually
           a G League player who shares a name with someone already on file.
  realgm   a RealGM player id in the G League transaction log that matched no
           one by name (tryouts, draft picks, international signings).
  twoway   a name on the league's two-way list that matched no one.

The site's Review tool (/admin/review) writes a decision per item to
Supabase's identity_decisions: link (he is this player), create (a new
player), ignore (not someone to track). `apply` carries those into DuckDB:

  link     the source id becomes one of the player's identifiers and the name
           one of his aliases, so the next time that source names him he is
           matched without asking; rows already held (G League box lines,
           G League moves, two-way list rows) get his player id
  create   a new player record first, then as link
  ignore   noted, and left out of the queue from then on

    python3 review_queue.py export            # build v2/review/queue.json
    python3 review_queue.py apply             # decisions from Supabase -> DuckDB
    python3 review_queue.py apply --sync      # on the Mac: lock, pull, apply, push, export
    python3 review_queue.py check             # counts, nothing written
"""
import argparse
import json
import re
import os
import sys
import tempfile
from datetime import datetime

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
os.chdir(HOME)
import nightly2 as N  # noqa: E402

PREFIX = "v2"
MAX_CANDIDATES = 6

DDL = """
CREATE TABLE IF NOT EXISTS identity_ignored (
    item_key VARCHAR PRIMARY KEY, name VARCHAR, decided_by VARCHAR, decided_at TIMESTAMP);
"""


def db():
    import duckdb
    con = duckdb.connect(N.DB_PATH)
    con.execute(DDL)
    return con


def norm(s):
    import ingest_rosters as R
    return R.norm(s)


def has_table(con, name):
    return con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0] > 0


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------

class Candidates:
    """Who an unresolved name might be: same name (or alias), or same surname
    and first initial, plus whoever the matcher proposed."""

    def __init__(self, con):
        self.by_name, self.by_key = {}, {}
        for pid, n in con.execute("""
                SELECT player_id, name_normalized FROM players WHERE name_normalized IS NOT NULL
                UNION SELECT player_id, alias_normalized FROM player_aliases WHERE alias_normalized IS NOT NULL""").fetchall():
            self.by_name.setdefault(n, set()).add(pid)
            k = self.key(n)
            if k:
                self.by_key.setdefault(k, set()).add(pid)
        self.info = {}
        for pid, name, bd in con.execute("SELECT player_id, full_name, birthdate FROM players").fetchall():
            self.info[pid] = {"player_id": pid, "name": name, "birthdate": str(bd) if bd else None}
        for src, sid, pid in con.execute("""SELECT source, source_id, player_id FROM player_identifiers
                                            WHERE source IN ('nba', 'bbref', 'realgm')""").fetchall():
            if pid in self.info:
                self.info[pid].setdefault(src + "_ids", []).append(sid)
        if has_table(con, "gl_player_game"):
            for pid, first, last, games, team in con.execute("""
                    SELECT player_id, MIN(season), MAX(season), COUNT(*) FILTER (WHERE played),
                           arg_max(team_abbr, game_date)
                    FROM gl_player_game WHERE player_id IS NOT NULL GROUP BY 1""").fetchall():
                if pid in self.info:
                    self.info[pid]["gl"] = {"first": first, "last": last, "games": games, "team": team}

    @staticmethod
    def key(n):
        parts = (n or "").replace("-", " ").split()
        parts = [p for p in parts if p not in ("jr", "sr", "ii", "iii", "iv")]
        return "%s %s" % (parts[0][0], parts[-1]) if len(parts) >= 2 else None

    def find(self, name, proposed=None):
        n = norm(name)
        out = []
        def add(pid, why):
            if pid in self.info and pid not in [c["player_id"] for c in out]:
                out.append(dict(self.info[pid], why=why))
        if proposed:
            add(proposed, "proposed by the matcher")
        for pid in sorted(self.by_name.get(n, ())):
            add(pid, "same name")
        for pid in sorted(self.by_key.get(self.key(n), ())):
            add(pid, "same surname and first initial")
        return out[:MAX_CANDIDATES]


def decided_keys(con):
    """Items with a decision already applied or ignored stay out of the queue."""
    return {k for (k,) in con.execute("SELECT item_key FROM identity_ignored").fetchall()}


def build_queue(con):
    cands = Candidates(con)
    gone = decided_keys(con)
    items = []

    # staged ids
    for src, sid, name, bd, team, league, n, first, last, proposed, score, why in con.execute("""
            SELECT source, source_id, raw_name, raw_birthdate, raw_team, raw_league, occurrences,
                   first_seen, last_seen, proposed_player_id, match_score, match_reasons
            FROM staging_players WHERE match_status = 'pending'
            ORDER BY last_seen DESC""").fetchall():
        key = "%s:%s" % (src, sid)
        if key in gone or not name:
            continue
        ctx = {"source": src, "source_id": sid, "team": team, "league": league,
               "birthdate": str(bd) if bd else None, "reason": why, "score": score,
               "last_seen": str(last)[:10] if last else None}
        if src == "nba" and has_table(con, "gl_player_game"):
            g = con.execute("""SELECT MIN(season), MAX(season), COUNT(*) FILTER (WHERE played),
                                      string_agg(DISTINCT team_abbr, ', ')
                               FROM gl_player_game WHERE person_id = ?""", [sid]).fetchone()
            if g and g[2]:
                ctx["gl"] = {"first": g[0], "last": g[1], "games": g[2], "teams": g[3]}
        items.append({"key": key, "kind": "staged", "name": name, "context": ctx,
                      "candidates": cands.find(name, proposed)})

    # RealGM ids that matched no one
    if has_table(con, "gl_transactions"):
        known = {sid for (sid,) in con.execute(
            "SELECT source_id FROM player_identifiers WHERE source = 'realgm'").fetchall()}
        for rid, name, moves in con.execute("""
                SELECT realgm_id, arg_max(name, txn_date),
                       list({'date': CAST(txn_date AS VARCHAR), 'kind': kind, 'text': raw_text} ORDER BY txn_date DESC)
                FROM gl_transactions
                WHERE realgm_id IS NOT NULL AND player_id IS NULL
                GROUP BY realgm_id ORDER BY MAX(txn_date) DESC""").fetchall():
            key = "realgm:%s" % rid
            if key in gone or rid in known:
                continue
            items.append({"key": key, "kind": "realgm", "name": name,
                          "context": {"source": "realgm", "source_id": rid, "moves": moves[:6],
                                      "url": "https://basketball.realgm.com/player/%s/Summary/%s" % (
                                          re.sub(r"[^A-Za-z0-9]+", "-", name or "x").strip("-"), rid)},
                          "candidates": cands.find(name)})

    # names on the two-way list
    if has_table(con, "twoway_list"):
        for name, team, gl in con.execute("""
                SELECT name, nba_team, gl_team FROM twoway_list
                WHERE snap_date = (SELECT MAX(snap_date) FROM twoway_list) AND player_id IS NULL""").fetchall():
            key = "twoway:%s" % norm(name)
            if key in gone:
                continue
            items.append({"key": key, "kind": "twoway", "name": name,
                          "context": {"source": "twoway", "team": team, "gl_team": gl},
                          "candidates": cands.find(name)})
    return items


def cmd_export(con, s3, bucket):
    # rows a decision has tied to a RealGM id, wherever they still lack a player
    if has_table(con, "gl_transactions"):
        con.execute("""UPDATE gl_transactions t SET player_id = i.player_id FROM player_identifiers i
                       WHERE i.source = 'realgm' AND i.source_id = t.realgm_id AND t.player_id IS NULL""")
        con.commit()
    items = build_queue(con)
    body = {"built_at": datetime.utcnow().isoformat() + "Z", "items": items}
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        with open(p, "w") as f:
            json.dump(body, f, default=str)
        s3.upload_file(p, bucket, "%s/review/queue.json" % PREFIX, ExtraArgs={"ContentType": "application/json"})
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print("  review queue: %d waiting — %s" % (len(items), ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))


# ---------------------------------------------------------------------------
# applying decisions
# ---------------------------------------------------------------------------

def pending_decisions(env):
    import sync_players_supabase as SP
    code, rows = SP.call(env, None, "identity_decisions?select=*&applied_at=is.null&order=decided_at")
    if code != 200:
        print("  could not read identity_decisions: HTTP %s %s" % (code, rows))
        return None
    return rows


def mark(env, did, outcome):
    import sync_players_supabase as SP
    code, body = SP.call(env, None, "identity_decisions?id=eq.%s" % did, "PATCH",
                         {"applied_at": datetime.utcnow().isoformat() + "Z", "outcome": outcome},
                         prefer="return=minimal")
    if code not in (200, 204):
        print("    could not mark decision %s applied: HTTP %s %s" % (did, code, body))


def new_player(con, name, birthdate=None):
    import ingest_rosters as R
    pid = R.next_ids(con, 1)[0]
    con.execute("""
        INSERT INTO players (player_id, full_name, display_name, name_normalized, birthdate,
               birthdate_status, status, provenance, created_at, updated_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, 'active', 'scraped', current_timestamp, current_timestamp, 'review')""",
        [pid, name, name, norm(name), birthdate, "confirmed" if birthdate else "missing"])
    return pid


def add_alias(con, pid, name, source):
    con.execute("""INSERT INTO player_aliases (player_id, alias, alias_normalized, source, first_seen)
                   VALUES (?, ?, ?, ?, current_timestamp) ON CONFLICT DO NOTHING""",
                [pid, name, norm(name), source])


def add_identifier(con, pid, source, sid):
    held = con.execute("SELECT player_id FROM player_identifiers WHERE source = ? AND source_id = ?",
                       [source, sid]).fetchone()
    if held and held[0] != pid:
        return "already belongs to %s" % held[0]
    if not held:
        con.execute("INSERT INTO player_identifiers (player_id, source, source_id) VALUES (?, ?, ?)",
                    [pid, source, sid])
    return None


def apply_one(con, d, touched):
    key, action = d["item_key"], d["action"]
    kind, _, sid = key.partition(":")
    name = d.get("item_name") or sid
    if action == "ignore":
        con.execute("INSERT INTO identity_ignored VALUES (?, ?, ?, now()) ON CONFLICT DO NOTHING",
                    [key, name, d.get("decided_by")])
        return "left out of the queue"

    birth = None
    if kind in ("nba", "bbref"):
        r = con.execute("SELECT raw_birthdate FROM staging_players WHERE source = ? AND source_id = ?",
                        [kind, sid]).fetchone()
        birth = r[0] if r else None
    if action == "create":
        pid = new_player(con, name, birth)
        did = "created %s" % pid
    else:
        pid = d["player_id"]
        if not con.execute("SELECT COUNT(*) FROM players WHERE player_id = ?", [pid]).fetchone()[0]:
            return "NOT APPLIED: no player %s" % pid
        did = "linked to %s" % pid

    if kind in ("nba", "bbref"):
        clash = add_identifier(con, pid, kind, sid)
        if clash:
            return "NOT APPLIED: %s id %s %s" % (kind, sid, clash)
        add_alias(con, pid, name, kind)
        con.execute("""UPDATE staging_players SET match_status = 'approved', proposed_player_id = ?,
                              resolved_at = now(), resolved_by = 'review'
                       WHERE source = ? AND source_id = ? AND match_status = 'pending'""", [pid, kind, sid])
        if kind == "nba" and has_table(con, "gl_player_game"):
            con.execute("UPDATE gl_player_game SET player_id = ? WHERE person_id = ? AND player_id IS NULL",
                        [pid, sid])
            touched.add(pid)
    elif kind == "realgm":
        clash = add_identifier(con, pid, "realgm", sid)
        if clash:
            return "NOT APPLIED: RealGM id %s %s" % (sid, clash)
        add_alias(con, pid, name, "realgm")
        con.execute("UPDATE gl_transactions SET player_id = ? WHERE realgm_id = ? AND player_id IS NULL", [pid, sid])
    elif kind == "twoway":
        add_alias(con, pid, name, "twoway")
        con.execute("UPDATE twoway_list SET player_id = ? WHERE player_id IS NULL AND name = ?", [pid, name])
    return did


def cmd_apply(con, env):
    rows = pending_decisions(env)
    if rows is None:
        return set()
    if not rows:
        print("  review: no decisions waiting")
        return set()
    touched, done = set(), []
    # the latest decision per item wins
    latest = {}
    for d in rows:
        latest[d["item_key"]] = d
    for d in rows:
        if latest[d["item_key"]] is not d:
            done.append((d["id"], "superseded by a later decision"))
            continue
        try:
            outcome = apply_one(con, d, touched)
        except Exception as e:
            outcome = "NOT APPLIED: %s" % str(e)[:160]
        done.append((d["id"], outcome))
        print("    %-24s %-7s %s" % ((d.get("item_name") or d["item_key"])[:24], d["action"], outcome))
    con.commit()
    for did, outcome in done:
        mark(env, did, outcome)
    print("  review: %d decisions applied" % sum(1 for _, o in done if not o.startswith("NOT")))
    return touched


def cmd_check(con):
    items = build_queue(con)
    kinds = {}
    for it in items:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print("  waiting: %d — %s" % (len(items), kinds))
    for it in items[:8]:
        print("    %-7s %-26s %d candidates  %s" % (it["kind"], it["name"][:26], len(it["candidates"]),
                                                 ", ".join("%s (%s)" % (c["name"], c["why"]) for c in it["candidates"][:2])))


def run(apply=True):
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    con = db()
    try:
        touched = cmd_apply(con, env) if apply else set()
        if touched:
            import ingest_gleague as G
            G.export_players(con, sorted(touched), s3, bucket)
            print("  re-exported %d G League game logs" % len(touched))
        cmd_export(con, s3, bucket)
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["export", "apply", "check"])
    ap.add_argument("--sync", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        con = db()
        try:
            cmd_check(con)
        finally:
            con.close()
        return
    if not a.sync:
        run(apply=a.cmd == "apply")
        return
    env = N.load_env()
    s3, bucket = N.s3c(env), env["R2_BUCKET_NAME"]
    log = N.Log()
    if not N.acquire_lock(s3, bucket, log):
        sys.exit("  another run holds the lock — try again when it finishes")
    try:
        if not N.pull_db(s3, bucket, log):
            sys.exit("  could not pull the database")
        run(apply=a.cmd == "apply")
        N.push_db(s3, bucket, log)
    finally:
        N.release_lock(s3, bucket)


if __name__ == "__main__":
    main()
