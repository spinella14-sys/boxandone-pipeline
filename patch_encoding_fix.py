#!/usr/bin/env python3
"""
patch_encoding_fix.py

Bug: "Luka Dončić" is stored as "Luka DonÄiÄ".

Cause: requests falls back to ISO-8859-1 for text/html when the server declares
no charset (RFC 2616). So r.text decoded UTF-8 bytes as Latin-1, and those
corrupted strings were then written out as UTF-8.

Consequence beyond looks: normalize_name() strips accents from whatever it is
handed, so "Dončić" normalized to "donaia" rather than "doncic". Every
non-ASCII matching key in the registry is wrong.

This script:
  1. Sets r.encoding = "utf-8" in seed_bbref.py and ingest_games.py so it
     cannot recur during the backfill.
  2. Repairs players.full_name / display_name / first_name / last_name and
     player_aliases.alias in place.
  3. Recomputes name_normalized and alias_normalized from the repaired names.

Mojibake is deterministic and reversible: s.encode('latin-1').decode('utf-8').
Strings that do not round-trip cleanly are left untouched.

  python3 patch_encoding_fix.py --dry-run
  python3 patch_encoding_fix.py
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
    """Reverse latin-1-decoded-UTF-8. Returns None when s is not mojibake."""
    if not s or all(ord(c) < 128 for c in s):
        return None
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if fixed == s:
        return None
    # sanity: repair should not introduce replacement chars
    if "\ufffd" in fixed:
        return None
    return fixed


# ---------------------------------------------------------------------------

def patch_scrapers():
    for fn in SCRAPERS:
        path = os.path.join(HOME, fn)
        if not os.path.exists(path):
            print(f"    {fn}: not found, skipped")
            continue
        src = open(path, encoding="utf-8").read()
        if 'r.encoding = "utf-8"' in src:
            print(f"    {fn}: already patched")
            continue

        before = src
        # seed_bbref.fetch() and ingest_games.get() both branch on status 200
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
            print(f"    {fn}: no matching pattern — CHECK MANUALLY")
            continue

        import ast
        try:
            ast.parse(src)
        except SyntaxError as e:
            print(f"    {fn}: patch invalid ({e}) — skipped")
            continue

        open(path + ".bak_enc", "w", encoding="utf-8").write(before)
        open(path, "w", encoding="utf-8").write(src)
        print(f"    {fn}: r.encoding set to utf-8")


def scan(con):
    rows = con.execute(
        "SELECT player_id, full_name, name_normalized FROM players").fetchall()
    hits = []
    for pid, name, norm in rows:
        fixed = demojibake(name)
        if fixed:
            hits.append((pid, name, fixed, norm))
    return hits


def repair(con, hits):
    sys.path.insert(0, HOME)
    from identity import normalize_name

    for pid, bad, good, _ in hits:
        n = normalize_name(good)
        con.execute("""
            UPDATE players SET full_name=?, display_name=?, first_name=?,
                   last_name=?, name_normalized=?, updated_at=now()
            WHERE player_id=?
        """, [good, good, n["first"], n["last"], n["name_key"], pid])

    # aliases
    alias_rows = con.execute(
        "SELECT player_id, alias, alias_normalized FROM player_aliases").fetchall()
    fixed_aliases = 0
    for pid, alias, anorm in alias_rows:
        good = demojibake(alias)
        if not good:
            continue
        n = normalize_name(good)
        con.execute("DELETE FROM player_aliases WHERE player_id=? AND alias_normalized=?",
                    [pid, anorm])
        con.execute("""
            INSERT INTO player_aliases (player_id, alias, alias_normalized, source)
            VALUES (?,?,?,'bbref') ON CONFLICT DO NOTHING
        """, [pid, good, n["name_key"]])
        fixed_aliases += 1
    con.commit()
    return fixed_aliases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import duckdb
    try:
        con = duckdb.connect(DB_PATH)
    except Exception as e:
        sys.exit(f"  cannot open database — fetch still running?\n  {str(e)[:110]}")

    hits = scan(con)
    print(f"  {len(hits)} corrupted names found\n")
    for pid, bad, good, norm in hits[:20]:
        print(f"    {bad:26} -> {good:24}   (key was {norm!r})")
    if len(hits) > 20:
        print(f"    ... and {len(hits)-20} more")

    if a.dry_run:
        con.close()
        print("\n  dry run — nothing changed")
        return

    if not hits:
        con.close()
        print("  nothing to repair")
        print("\n  patching scrapers anyway:")
        patch_scrapers()
        return

    con.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{DB_PATH}.bak_enc_{stamp}"
    shutil.copy2(DB_PATH, backup)
    print(f"\n  backup -> {os.path.basename(backup)}")

    con = duckdb.connect(DB_PATH)
    try:
        n_alias = repair(con, hits)
    except Exception as e:
        con.close()
        shutil.copy2(backup, DB_PATH)
        sys.exit(f"  REPAIR FAILED — database restored\n  {e}")

    remaining = len(scan(con))
    sample = con.execute(
        "SELECT full_name, name_normalized FROM players "
        "WHERE full_name LIKE '%Jok%' OR full_name LIKE '%Don%i%' LIMIT 5").fetchall()
    con.close()

    print(f"  repaired {len(hits)} names, {n_alias} aliases")
    print(f"  remaining corrupted: {remaining}")
    print("  sample:")
    for r in sample:
        print(f"    {r[0]:26} -> {r[1]}")

    print("\n  patching scrapers so it cannot recur:")
    patch_scrapers()


if __name__ == "__main__":
    main()
