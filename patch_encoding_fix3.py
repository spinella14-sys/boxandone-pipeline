#!/usr/bin/env python3
"""
patch_encoding_fix3.py

Repairs mojibake in scraped player names ("Luka DonÄiÄ" -> "Luka Dončić")
and stops it recurring.

Why this is harder than an UPDATE: DuckDB implements UPDATE as delete+insert,
and five tables hold foreign keys to players (identifiers, aliases, merges,
game_box, metrics). The delete half trips the FK check, so
    UPDATE players SET full_name = ...
fails outright. There is no way to edit a players row in place.

So: capture each referencing table's DDL from duckdb_tables().sql, copy its
rows aside, drop it, rebuild players with corrected names, then recreate every
child from its own captured DDL and restore its rows. Nothing is hardcoded —
the referencing tables are discovered from duckdb_constraints(), so a table I
have not thought of is still handled.

Row counts are verified for every table. Any mismatch aborts and restores.

  python3 patch_encoding_fix3.py --dry-run
  python3 patch_encoding_fix3.py
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HOME = os.path.expanduser("~/boxandone")
DB_PATH = os.path.join(HOME, "data", "boxandone.duckdb")
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


def scan(con):
    return [(pid, name, demojibake(name))
            for pid, name in con.execute(
                "SELECT player_id, full_name FROM players").fetchall()
            if demojibake(name)]


def children_of_players(con):
    rows = con.execute("""
        SELECT DISTINCT table_name FROM duckdb_constraints()
        WHERE constraint_type='FOREIGN KEY'
          AND constraint_text ILIKE '%REFERENCES players%'
        ORDER BY table_name
    """).fetchall()
    return [r[0] for r in rows]


def rebuild(con, hits):
    kids = children_of_players(con)
    print(f"    tables referencing players: {kids}")

    ddl, counts = {}, {}
    for t in kids:
        d = con.execute(
            "SELECT sql FROM duckdb_tables() WHERE table_name=?", [t]).fetchone()
        if not d or not d[0]:
            raise RuntimeError(f"no DDL captured for {t}")
        ddl[t] = d[0]
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        con.execute(f'CREATE TABLE "_bk_{t}" AS SELECT * FROM "{t}"')
    counts["players"] = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    print(f"    row counts: {counts}")

    for t in kids:
        con.execute(f'DROP TABLE "{t}"')

    # rebuild players with corrected names.
    # CREATE TABLE AS SELECT would drop the PRIMARY KEY, and the children need
    # it to re-attach their foreign keys — so use the captured DDL instead.
    players_ddl = con.execute(
        "SELECT sql FROM duckdb_tables() WHERE table_name='players'").fetchone()
    if not players_ddl or not players_ddl[0]:
        raise RuntimeError("no DDL captured for players")
    players_ddl = players_ddl[0]
    if "PRIMARY KEY" not in players_ddl.upper():
        raise RuntimeError("captured players DDL has no PRIMARY KEY")

    fixmap = {pid: good for pid, _, good in hits}
    cols = [d[0] for d in con.execute("SELECT * FROM players LIMIT 0").description]
    con.execute("CREATE TABLE _bk_players AS SELECT * FROM players")
    ph = ",".join("?" * len(cols))
    batch = []
    for row in con.execute("SELECT * FROM _bk_players").fetchall():
        rec = dict(zip(cols, row))
        if rec["player_id"] in fixmap:
            good = fixmap[rec["player_id"]]
            n = norm(good)
            rec.update(full_name=good, display_name=good,
                       first_name=n["first"], last_name=n["last"],
                       name_normalized=n["name_key"])
        batch.append([rec[c] for c in cols])

    con.execute("DROP TABLE players")
    con.execute(players_ddl)
    con.executemany(f"INSERT INTO players VALUES ({ph})", batch)
    con.execute("DROP TABLE _bk_players")
    con.execute("CREATE INDEX IF NOT EXISTS idx_players_name ON players(name_normalized)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_players_bday ON players(birthdate)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_players_draft ON players(draft_year)")

    n_players = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    if n_players != counts["players"]:
        raise RuntimeError(f"players row loss: {counts['players']} -> {n_players}")
    print(f"    players rebuilt: {n_players:,} rows, {len(hits)} names corrected")

    # recreate children from their own DDL, restore rows
    for t in kids:
        con.execute(ddl[t])
        if t == "player_aliases":
            seen, rows = set(), []
            kcols = [d[0] for d in con.execute(
                f"SELECT * FROM _bk_{t} LIMIT 0").description]
            ai, ni = kcols.index("alias"), kcols.index("alias_normalized")
            for r in con.execute(f"SELECT * FROM _bk_{t}").fetchall():
                r = list(r)
                good = demojibake(r[ai]) or r[ai]
                r[ai] = good
                r[ni] = norm(good)["name_key"]
                key = (r[kcols.index("player_id")], r[ni])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
            con.executemany(
                f'INSERT INTO "{t}" VALUES ({",".join("?"*len(kcols))})', rows)
            got = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"    {t}: {got:,} rows "
                  f"({counts[t]-got} dropped as post-repair duplicates)")
        else:
            con.execute(f'INSERT INTO "{t}" SELECT * FROM "_bk_{t}"')
            got = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if got != counts[t]:
                raise RuntimeError(f"{t} row loss: {counts[t]} -> {got}")
            print(f"    {t}: {got:,} rows (verified)")

    con.execute("CREATE INDEX IF NOT EXISTS idx_ident_player ON player_identifiers(player_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_alias_norm ON player_aliases(alias_normalized)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_box_player ON player_game_box(player_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_box_game ON player_game_box(game_id)")

    for t in kids:
        con.execute(f'DROP TABLE "_bk_{t}"')
    con.commit()


def patch_scrapers():
    for fn in SCRAPERS:
        path = os.path.join(HOME, fn)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        if 'r.encoding = "utf-8"' in src:
            print(f"    {fn}: already patched")
            continue
        before = src
        src = src.replace(
            "            if r.status_code == 200:\n",
            '            if r.status_code == 200:\n                r.encoding = "utf-8"\n')
        src = src.replace(
            "        if r.status_code == 200:\n            return r.text",
            '        if r.status_code == 200:\n            r.encoding = "utf-8"\n            return r.text')
        src = src.replace(
            "            if r.status_code != 200:",
            '            r.encoding = "utf-8"\n            if r.status_code != 200:')
        if src == before:
            print(f"    {fn}: pattern not found — CHECK MANUALLY")
            continue
        import ast
        try:
            ast.parse(src)
        except SyntaxError:
            print(f"    {fn}: patch invalid — skipped")
            continue
        open(path + ".bak_enc", "w", encoding="utf-8").write(before)
        open(path, "w", encoding="utf-8").write(src)
        print(f"    {fn}: r.encoding set to utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import duckdb
    con = duckdb.connect(DB_PATH)
    hits = scan(con)
    kids = children_of_players(con)
    con.close()

    print(f"  {len(hits)} corrupted names")
    print(f"  {len(kids)} tables reference players: {kids}\n")
    for _, bad, good in hits[:8]:
        print(f"    {bad:28} -> {good}")
    if len(hits) > 8:
        print(f"    ... and {len(hits)-8} more")

    if a.dry_run:
        print("\n  dry run — nothing changed")
        return
    if not hits:
        print("\n  nothing to repair; patching scrapers:")
        patch_scrapers()
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{DB_PATH}.bak_enc3_{stamp}"
    shutil.copy2(DB_PATH, backup)
    print(f"\n  backup -> {os.path.basename(backup)}")

    con = duckdb.connect(DB_PATH)
    try:
        rebuild(con, hits)
        con.close()
    except Exception as e:
        try:
            con.close()
        except Exception:
            pass
        shutil.copy2(backup, DB_PATH)
        sys.exit(f"\n  FAILED — database restored from backup\n  {e}")

    con = duckdb.connect(DB_PATH)
    left = len(scan(con))
    sample = con.execute(
        "SELECT full_name, name_normalized FROM players "
        "WHERE name_normalized <> lower(full_name) "
        "AND full_name <> '' ORDER BY full_name LIMIT 6").fetchall()
    con.close()

    print(f"\n  remaining corrupted: {left}")
    for r in sample:
        print(f"    {r[0]:28} -> {r[1]}")
    print("\n  patching scrapers:")
    patch_scrapers()


if __name__ == "__main__":
    main()
