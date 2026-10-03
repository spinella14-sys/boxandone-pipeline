#!/usr/bin/env python3
"""
patch_fetch_lock.py

Fixes: ingest_games.py's fetch phase holds the DuckDB write lock for the entire
run. Two hours for a season, three days for the backfill — during which nothing
else can open the database.

New behavior: read the pending list, close the connection, fetch to disk with
NO lock held, and reopen briefly every 25 games to record progress. Lock is held
for milliseconds at a time instead of days.

Progress is still durable. A Ctrl-C flushes the pending batch before exiting,
and any game whose HTML is already on disk is marked fetched on the next run
regardless.

Run:  python3 patch_fetch_lock.py
Cannot run while a fetch is in progress. Wait for it to finish.
"""

import os
import sys

TARGET = os.path.expanduser("~/boxandone/ingest_games.py")

NEW_FETCH = '''def phase_fetch(limit=None):
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Read the work list, then RELEASE the lock for the duration of the fetch.
    con = db()
    rows = con.execute("""
        SELECT game_id, source_slug FROM ingest_log
        WHERE NOT fetched ORDER BY game_date DESC
    """).fetchall()
    con.close()

    if limit:
        rows = rows[:limit]
    if not rows:
        print("  nothing pending")
        return

    eta = len(rows) * (REQUEST_DELAY + JITTER / 2) / 3600
    print(f"  {len(rows):,} games pending  (~{eta:.1f} hours)")
    print("  database is NOT locked during fetch — other tools can run")
    print("  Ctrl-C is safe — progress flushes before exit\\n")

    def flush(ok_ids, err_ids):
        if not ok_ids and not err_ids:
            return
        c = db()
        for gid in ok_ids:
            c.execute("UPDATE ingest_log SET fetched=TRUE, fetch_error=NULL, "
                      "updated_at=now() WHERE game_id=?", [gid])
        for gid, msg in err_ids:
            c.execute("UPDATE ingest_log SET fetch_error=?, updated_at=now() "
                      "WHERE game_id=?", [msg, gid])
        c.commit()
        c.close()
        ok_ids.clear()
        err_ids.clear()

    pending_ok, pending_err = [], []
    done = fail = 0

    try:
        for i, (game_id, slug) in enumerate(rows, 1):
            path = box_path(slug)
            if os.path.exists(path):
                pending_ok.append(game_id)
            else:
                html = get(session, f"{BASE}/boxscores/{slug}.html")
                polite_sleep()
                if html is None:
                    pending_err.append((game_id, "not found"))
                    fail += 1
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with gzip.open(path, "wt", encoding="utf-8") as f:
                        f.write(html)
                    pending_ok.append(game_id)
                    done += 1

            if i % 25 == 0:
                flush(pending_ok, pending_err)
                remain = (len(rows) - i) * (REQUEST_DELAY + JITTER / 2) / 3600
                print(f"  {i:,}/{len(rows):,}  ok={done} fail={fail}  ~{remain:.1f}h left")
    except KeyboardInterrupt:
        print("\\n  interrupted — flushing progress")
    finally:
        flush(pending_ok, pending_err)

    print(f"\\n  fetched {done:,}, failed {fail}")
'''


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"{TARGET} not found")

    src = open(TARGET, encoding="utf-8").read()

    if "database is NOT locked during fetch" in src:
        print("  already patched")
        return

    start = src.find("def phase_fetch(limit=None):")
    if start == -1:
        sys.exit("  could not locate phase_fetch — file may have changed")

    # phase_fetch ends at the next top-level marker
    marker = "\n# ---------------------------------------------------------------------------\n# PHASE 3"
    end = src.find(marker, start)
    if end == -1:
        sys.exit("  could not locate end of phase_fetch")

    open(TARGET + ".bak_lock", "w", encoding="utf-8").write(src)
    patched = src[:start] + NEW_FETCH + "\n" + src[end + 1:]
    open(TARGET, "w", encoding="utf-8").write(patched)

    import ast
    try:
        ast.parse(patched)
    except SyntaxError as e:
        open(TARGET, "w", encoding="utf-8").write(src)
        sys.exit(f"  patch produced invalid syntax ({e}) — reverted, nothing changed")

    print("  phase_fetch replaced with batched-write version")
    print("  backup -> ingest_games.py.bak_lock")


if __name__ == "__main__":
    main()
