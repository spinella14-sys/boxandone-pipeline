#!/usr/bin/env python3
"""
patch_fetch_retry.py

Bug: the fetch died after 5,675 games with a DuckDB lock error.

My batched-write patch releases the lock between batches, which fixed the
"database locked for three days" problem. But it never handled the reverse:
another process (a concurrent parse) holding the lock at the moment flush()
tries to reopen. flush() called db(), got an IOException, and the whole
fetch crashed — taking ~45 hours of remaining work with it.

Fix: flush() retries with backoff instead of raising. Six attempts over about
two and a half minutes, which comfortably outlasts a parse batch. If it still
cannot get in, the pending IDs are kept in the buffer and retried at the next
flush rather than being dropped.

Nothing is lost when this happens anyway — the HTML is already on disk, and
a game whose file exists is marked fetched on the next run.

Run:  python3 patch_fetch_retry.py
Cannot run while a fetch is in progress.
"""

import ast
import os
import sys

TARGET = os.path.expanduser("~/boxandone/ingest_games.py")

OLD = '''    def flush(ok_ids, err_ids):
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
        err_ids.clear()'''

NEW = '''    def flush(ok_ids, err_ids, final=False):
        """Write progress. Retries on lock contention rather than crashing.

        Another process (usually a concurrent parse) can hold the write lock.
        Losing a flush is harmless — the HTML is on disk and gets marked
        fetched on the next run — but crashing the fetch is not.
        """
        if not ok_ids and not err_ids:
            return
        delays = [2, 5, 15, 30, 60, 90]
        for attempt, wait in enumerate(delays, 1):
            try:
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
                return
            except Exception as e:
                if attempt == 1:
                    print(f"  db busy ({str(e)[:60]}) — retrying")
                if attempt < len(delays):
                    time.sleep(wait)
                else:
                    print(f"  could not write progress after {len(delays)} tries; "
                          f"{len(ok_ids)} game(s) held for next flush")
                    if final:
                        print("  (harmless — files are on disk and will be "
                              "marked fetched on the next run)")
                    return'''


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"{TARGET} not found")
    src = open(TARGET, encoding="utf-8").read()

    if "delays = [2, 5, 15, 30, 60, 90]" in src:
        print("  already patched")
        return
    if OLD not in src:
        sys.exit("  flush() does not match expected form — file differs")

    src = src.replace(OLD, NEW)
    src = src.replace("        flush(pending_ok, pending_err)\n\n    print(f\"\\n  fetched",
                      "        flush(pending_ok, pending_err, final=True)\n\n    print(f\"\\n  fetched")

    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit(f"  would produce invalid syntax ({e}) — nothing changed")
    if "import time" not in src:
        sys.exit("  'time' not imported — unexpected, nothing changed")

    open(TARGET + ".bak_retry", "w", encoding="utf-8").write(
        open(TARGET, encoding="utf-8").read())
    open(TARGET, "w", encoding="utf-8").write(src)
    print("  flush() now retries on lock contention (6 tries over ~3.5 min)")
    print("  backup -> ingest_games.py.bak_retry")


if __name__ == "__main__":
    main()
