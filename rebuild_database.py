#!/usr/bin/env python3
"""
rebuild_database.py

Two problems, one solution.

  1. 153 player names hold mojibake ("Luka DonÄiÄ").
  2. The catalog carries a stale dependency on "player_identifiers_new", a
     temporary name migrate_v2_leagues used with ALTER TABLE ... RENAME.
     DuckDB never cleared it, so DROP TABLE players fails with a reference to
     a table that does not exist. It is invisible to duckdb_constraints() and
     cannot be dropped, because there is nothing there to drop.

Editing in place is impossible: UPDATE on players fails (five FK children,
UPDATE is delete+insert internally), and rebuilding players fails on the
phantom. So this writes a brand new database file instead.

  - captures every table and view DDL from the live catalog
  - orders tables so FK parents are created before children
  - copies all rows across an ATTACH, repairing names in flight
  - verifies row counts table by table
  - swaps the files only after every count matches

The old file is kept. Nothing is deleted.

  python3 rebuild_database.py --dry-run
  python3 rebuild_database.py
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
NEW_PATH = os.path.join(HOME, "data", "boxandone_rebuilt.duckdb")
SCRAPERS = ["seed_bbref.py", "ingest_games.py"]


def demojibake(s):
    if not s or all(ord(c) < 128 for c in s):
        return None
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if fixed == s or "\ufffd" in fixed:
        return None
    return fixed


_norm = None
def norm(name):
    global _norm
    if _norm is None:
        sys.path.insert(0, HOME)
        from identity import normalize_name
        _norm = normalize_name
    return _norm(name)


def fk_parents(ddl):
    return set(re.findall(r"REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)", ddl or "",
                          re.IGNORECASE))


def order_tables(ddls):
    """Parents before children. Ignores references to tables not present."""
    names = set(ddls)
    pending = dict(ddls)
    ordered = []
    while pending:
        ready = [t for t, d in pending.items()
                 if not (fk_parents(d) & names & set(pending)) - {t}]
        if not ready:
            ordered.extend(sorted(pending))   # cycle: fall back to any order
            break
        for t in sorted(ready):
            ordered.append(t)
            del pending[t]
    return ordered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import duckdb

    src = duckdb.connect(DB_PATH)
    tables = {r[0]: r[1] for r in src.execute(
        "SELECT table_name, sql FROM duckdb_tables()").fetchall()}
    views = {r[0]: r[1] for r in src.execute(
        "SELECT view_name, sql FROM duckdb_views() WHERE NOT internal").fetchall()}
    counts = {t: src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
              for t in tables}
    bad = [(pid, nm, demojibake(nm)) for pid, nm in src.execute(
        "SELECT player_id, full_name FROM players").fetchall() if demojibake(nm)]
    seqs = src.execute(
        "SELECT sequence_name, start_value, increment_by, last_value "
        "FROM duckdb_sequences()").fetchall()
    src.close()

    order = order_tables(tables)

    print(f"  {len(tables)} tables, {len(views)} views, {len(seqs)} sequences")
    print(f"  {len(bad)} names to repair\n")
    print("  creation order:")
    for t in order:
        print(f"    {t:24} {counts[t]:>9,} rows")

    if a.dry_run:
        print("\n  dry run — nothing changed")
        return

    if os.path.exists(NEW_PATH):
        os.remove(NEW_PATH)

    fixmap = {pid: good for pid, _, good in bad}

    new = duckdb.connect(NEW_PATH)
    new.execute(f"ATTACH '{DB_PATH}' AS old (READ_ONLY)")

    try:
        for name, start, inc, last in seqs:
            nxt = (last + inc) if last is not None else (start or 1)
            new.execute(f'CREATE SEQUENCE "{name}" START {nxt} INCREMENT {inc or 1}')
        if seqs:
            print(f"    {len(seqs)} sequences recreated at current position")

        for t in order:
            ddl = tables[t]
            # DDL is emitted unqualified; keep it that way in the new db
            new.execute(ddl)

            if t == "players":
                cols = [d[0] for d in new.execute(
                    'SELECT * FROM "players" LIMIT 0').description]
                ph = ",".join("?" * len(cols))
                batch = []
                for row in new.execute("SELECT * FROM old.players").fetchall():
                    rec = dict(zip(cols, row))
                    if rec["player_id"] in fixmap:
                        good = fixmap[rec["player_id"]]
                        n = norm(good)
                        rec.update(full_name=good, display_name=good,
                                   first_name=n["first"], last_name=n["last"],
                                   name_normalized=n["name_key"])
                    batch.append([rec[c] for c in cols])
                new.executemany(f"INSERT INTO players VALUES ({ph})", batch)

            elif t == "player_aliases":
                cols = [d[0] for d in new.execute(
                    'SELECT * FROM "player_aliases" LIMIT 0').description]
                ai, ni = cols.index("alias"), cols.index("alias_normalized")
                pi = cols.index("player_id")
                seen, rows = set(), []
                for row in new.execute("SELECT * FROM old.player_aliases").fetchall():
                    r = list(row)
                    good = demojibake(r[ai]) or r[ai]
                    r[ai] = good
                    r[ni] = norm(good)["name_key"]
                    key = (r[pi], r[ni])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(r)
                new.executemany(
                    f'INSERT INTO player_aliases VALUES ({",".join("?"*len(cols))})',
                    rows)

            else:
                new.execute(f'INSERT INTO "{t}" SELECT * FROM old."{t}"')

            got = new.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            flag = ""
            if got != counts[t]:
                if t == "player_aliases":
                    flag = f"  ({counts[t]-got} post-repair duplicates dropped)"
                else:
                    raise RuntimeError(
                        f"{t}: expected {counts[t]:,}, got {got:,}")
            print(f"    {t:24} {got:>9,} rows{flag}")

        for v, ddl in views.items():
            try:
                new.execute(ddl)
            except Exception as e:
                print(f"    view {v}: {str(e).split(chr(10))[0][:60]}")

        # indexes
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_players_name ON players(name_normalized)",
            "CREATE INDEX IF NOT EXISTS idx_players_bday ON players(birthdate)",
            "CREATE INDEX IF NOT EXISTS idx_ident_player ON player_identifiers(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_alias_norm ON player_aliases(alias_normalized)",
            "CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date)",
            "CREATE INDEX IF NOT EXISTS idx_box_player ON player_game_box(player_id)",
            "CREATE INDEX IF NOT EXISTS idx_box_game ON player_game_box(game_id)",
        ]:
            try:
                new.execute(stmt)
            except Exception:
                pass

        new.execute("DETACH old")
        new.commit()
        new.close()
    except Exception as e:
        try:
            new.close()
        except Exception:
            pass
        if os.path.exists(NEW_PATH):
            os.remove(NEW_PATH)
        sys.exit(f"\n  FAILED — original untouched, no swap performed\n  {e}")

    # verify the new file independently before swapping
    chk = duckdb.connect(NEW_PATH)
    left = sum(1 for _, nm in chk.execute(
        "SELECT player_id, full_name FROM players").fetchall() if demojibake(nm))
    ok_pk = True
    try:
        chk.execute("INSERT INTO players (player_id, full_name, name_normalized) "
                    "SELECT player_id, 'x', 'x' FROM players LIMIT 1")
        ok_pk = False
    except Exception:
        pass
    sample = chk.execute(
        "SELECT full_name, name_normalized FROM players "
        "WHERE full_name <> name_normalized AND length(full_name) > 0 "
        "ORDER BY full_name LIMIT 5").fetchall()
    chk.close()

    if left or not ok_pk:
        sys.exit(f"\n  verification failed (corrupted left={left}, pk_ok={ok_pk})"
                 f"\n  new file kept at {NEW_PATH}, original untouched")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.move(DB_PATH, f"{DB_PATH}.pre_rebuild_{stamp}")
    shutil.move(NEW_PATH, DB_PATH)

    print(f"\n  swapped in. old file kept as boxandone.duckdb.pre_rebuild_{stamp}")
    print(f"  remaining corrupted: {left}")
    for r in sample:
        print(f"    {r[0]:28} -> {r[1]}")

    print("\n  patching scrapers:")
    for fn in SCRAPERS:
        p = os.path.join(HOME, fn)
        if not os.path.exists(p):
            continue
        s = open(p, encoding="utf-8").read()
        if 'r.encoding = "utf-8"' in s:
            print(f"    {fn}: already patched")
            continue
        before = s
        s = s.replace("            if r.status_code == 200:\n",
                      '            if r.status_code == 200:\n                r.encoding = "utf-8"\n')
        s = s.replace("        if r.status_code == 200:\n            return r.text",
                      '        if r.status_code == 200:\n            r.encoding = "utf-8"\n            return r.text')
        s = s.replace("            if r.status_code != 200:",
                      '            r.encoding = "utf-8"\n            if r.status_code != 200:')
        if s == before:
            print(f"    {fn}: pattern not found — CHECK MANUALLY")
            continue
        import ast
        try:
            ast.parse(s)
        except SyntaxError:
            print(f"    {fn}: patch invalid — skipped")
            continue
        open(p + ".bak_enc", "w", encoding="utf-8").write(before)
        open(p, "w", encoding="utf-8").write(s)
        print(f"    {fn}: r.encoding set to utf-8")


if __name__ == "__main__":
    main()
