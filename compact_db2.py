#!/usr/bin/env python3
"""
compact_db2.py — reclaim disk after archive_cold.py prune, FK-aware.

v1 used COPY FROM DATABASE, which copies tables in arbitrary order. It tried to
write player_game_box before players existed and the foreign key rejected it.

This uses the ordered-copy approach that rebuild_database.py already proved on
this schema: capture each table's DDL from duckdb_tables().sql, sort so
foreign-key parents are created and filled before their children, then copy.

Writes a new file, verifies every table's row count, and only then swaps. The
original is kept as .prepack until you delete it.

    python3 compact_db2.py
    python3 compact_db2.py --dry-run     # show the copy order, change nothing
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
NEW_PATH = DB_PATH + ".compacting"


def fk_parents(ddl):
    return set(re.findall(r"REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)",
                          ddl or "", re.IGNORECASE))


def order_tables(ddls):
    """Parents before children. Self-references and unknown targets ignored."""
    names = set(ddls)
    pending = dict(ddls)
    out = []
    while pending:
        ready = [t for t, d in pending.items()
                 if not ((fk_parents(d) & names & set(pending)) - {t})]
        if not ready:
            out.extend(sorted(pending))      # cycle: give up on ordering
            break
        for t in sorted(ready):
            out.append(t)
            del pending[t]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import duckdb

    if not os.path.exists(DB_PATH):
        sys.exit("  %s not found" % DB_PATH)
    if os.path.exists(NEW_PATH):
        os.remove(NEW_PATH)

    before = os.path.getsize(DB_PATH)
    print("  current: %.2f GB" % (before / 1073741824.0))

    con = duckdb.connect(DB_PATH, read_only=True)
    ddls = {r[0]: r[1] for r in con.execute(
        "SELECT table_name, sql FROM duckdb_tables()").fetchall()}
    views = {r[0]: r[1] for r in con.execute(
        "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal").fetchall()}
    seqs = con.execute("SELECT sequence_name, start_value, increment_by, last_value "
                       "FROM duckdb_sequences()").fetchall()
    counts = {t: con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
              for t in ddls}
    con.close()

    order = order_tables(ddls)
    print("  %d tables, %d views, %d sequences\n" % (len(ddls), len(views), len(seqs)))
    print("  copy order (parents first):")
    for t in order:
        deps = sorted(fk_parents(ddls[t]) & set(ddls) - {t})
        dep = ("  <- " + ", ".join(deps)) if deps else ""
        print("    %-22s %12d rows%s" % (t, counts[t], dep))

    if a.dry_run:
        print("\n  dry run — nothing changed")
        return

    print("\n  rewriting...")
    new = duckdb.connect(NEW_PATH)
    new.execute("ATTACH '%s' AS src (READ_ONLY)" % DB_PATH)
    try:
        for name, start, inc, last in seqs:
            nxt = (last + inc) if last is not None else (start or 1)
            new.execute('CREATE SEQUENCE "%s" START %d INCREMENT %d'
                        % (name, nxt, inc or 1))

        for t in order:
            new.execute(ddls[t])
            new.execute('INSERT INTO "%s" SELECT * FROM src."%s"' % (t, t))
            got = new.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            if got != counts[t]:
                raise RuntimeError("%s: %d -> %d" % (t, counts[t], got))
            print("    %-22s %12d rows" % (t, got))

        for v, ddl in views.items():
            try:
                new.execute(ddl)
            except Exception as e:
                print("    view %-18s skipped (%s)" % (v, str(e).split("\n")[0][:50]))

        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_players_name ON players(name_normalized)",
            "CREATE INDEX IF NOT EXISTS idx_players_bday ON players(birthdate)",
            "CREATE INDEX IF NOT EXISTS idx_ident_player ON player_identifiers(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_alias_norm ON player_aliases(alias_normalized)",
            "CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date)",
            "CREATE INDEX IF NOT EXISTS idx_box_player ON player_game_box(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_box_game ON player_game_box(game_id)",
            "CREATE INDEX IF NOT EXISTS idx_pbp_game ON play_by_play(game_id)",
            "CREATE INDEX IF NOT EXISTS idx_pbp_player ON play_by_play(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_pbp_group ON play_by_play(game_id, event_group)",
        ]:
            try:
                new.execute(stmt)
            except Exception:
                pass

        new.execute("DETACH src")
        new.close()
    except Exception as e:
        try:
            new.close()
        except Exception:
            pass
        if os.path.exists(NEW_PATH):
            os.remove(NEW_PATH)
        sys.exit("\n  FAILED: %s\n  original untouched" % str(e)[:200])

    # independent re-open and verify before swapping
    chk = duckdb.connect(NEW_PATH, read_only=True)
    bad = []
    for t in ddls:
        try:
            n = chk.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        except Exception:
            bad.append("%s MISSING" % t)
            continue
        if n != counts[t]:
            bad.append("%s %d -> %d" % (t, counts[t], n))
    got_views = [r[0] for r in chk.execute(
        "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
    chk.close()

    if bad:
        print("\n  VERIFICATION FAILED — not swapping:")
        for b in bad[:10]:
            print("    %s" % b)
        print("  new file left at %s" % NEW_PATH)
        return

    after = os.path.getsize(NEW_PATH)
    missing = [v for v in views if v not in got_views]
    print("\n  verified: %d tables, all counts match; %d/%d views"
          % (len(ddls), len(got_views), len(views)))
    if missing:
        print("  views not carried: %s" % ", ".join(missing))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old = "%s.prepack_%s" % (DB_PATH, stamp)
    shutil.move(DB_PATH, old)
    shutil.move(NEW_PATH, DB_PATH)

    print("\n  %.2f GB -> %.2f GB  (freed %.2f GB)"
          % (before / 1073741824.0, after / 1073741824.0,
             (before - after) / 1073741824.0))
    print("  original kept: %s" % os.path.basename(old))
    print("  delete once satisfied:  rm %s" % old)


if __name__ == "__main__":
    main()
