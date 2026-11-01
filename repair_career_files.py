#!/usr/bin/env python3
"""
repair_career_files.py — fix the player career files the nightly has been
inflating, and the nightly itself.

What went wrong. Each player's career files in R2 (players/{id}/gamelog,
shots, assists) are merged each night: keep the old file's rows except the
current season, then add the hot database's rows. But the hot database holds
two seasons, 2025-26 and 2026-27, so every night a player played, his 2025-26
rows were added again on top of the copy already there. Adem Bona's file held
about nine copies of his 2025-26 season. The database itself is clean
(audit_stats.py: play-by-play matches the box score for every player-season);
only these files are wrong.

  python3 repair_career_files.py check    # count inflated files, change nothing
  python3 repair_career_files.py build    # rebuild every affected file, patch the nightly

build rewrites all three files, whole and from the archive plus the hot
database, for everyone who has played in a season the hot database holds,
then patches nightly2.py so the merge replaces every season the new rows
cover, not just the current one.
"""
import os
import sys
import tempfile

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
sys.path.insert(0, HOME)
KINDS = ("gamelog", "shots", "assists")


def players_in_hot(con):
    seasons = [r[0] for r in con.execute("SELECT DISTINCT season FROM hot.games ORDER BY 1").fetchall()]
    ids = [r[0] for r in con.execute("""
        SELECT DISTINCT b.player_id FROM hot.player_game_box b
        JOIN hot.games g ON g.game_id = b.game_id WHERE b.played ORDER BY 1""").fetchall()]
    return seasons, ids


def check(con, s3, bucket, ids, X):
    """Rows per season in each R2 file against what the database says."""
    bad = {k: 0 for k in KINDS[:2]}
    worst = []
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "f.parquet")
        for i, pid in enumerate(ids, 1):
            for kind, sql in (("gamelog", X.GAMELOG_SQL), ("shots", X.SHOTS_SQL)):
                try:
                    s3.download_file(bucket, "%s/players/%s/%s.parquet" % (X.PREFIX, pid, kind), p)
                except Exception:
                    continue
                have = dict(con.execute("SELECT season, COUNT(*) FROM read_parquet(?) GROUP BY 1", [p]).fetchall())
                want = dict(con.execute("SELECT season, COUNT(*) FROM (%s) GROUP BY 1" % (sql % pid)).fetchall())
                extra = sum(max(0, have.get(s, 0) - n) for s, n in want.items())
                if extra:
                    bad[kind] += 1
                    worst.append((extra, kind, pid))
            if i % 100 == 0:
                print("    checked %d / %d" % (i, len(ids)), flush=True)
    print("\n  files with extra rows: gamelog %d, shots %d (of %d players)" % (bad["gamelog"], bad["shots"], len(ids)))
    names = dict(con.execute("SELECT player_id, full_name FROM hot.players").fetchall())
    for extra, kind, pid in sorted(worst, reverse=True)[:12]:
        print("    %-8s %-11s %-24s %6d extra rows" % (kind, pid, names.get(pid, ""), extra))


NIGHTLY_OLD = """                        COPY (SELECT * FROM read_parquet('%s')
                              WHERE season <> '%s'
                              UNION ALL BY NAME
                              SELECT * FROM read_parquet('%s'))
                        TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)\"\"\"
                        % (old, season, new, out))"""
NIGHTLY_NEW = """                        COPY (SELECT * FROM read_parquet('%s')
                              -- every season the new rows cover is replaced,
                              -- not only the current one: the hot database
                              -- can hold more than one season
                              WHERE season NOT IN (SELECT DISTINCT season FROM read_parquet('%s'))
                              UNION ALL BY NAME
                              SELECT * FROM read_parquet('%s'))
                        TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)\"\"\"
                        % (old, new, new, out))"""


def patch_nightly():
    path = os.path.join(HOME, "nightly2.py")
    src = open(path).read()
    if "every season the new rows cover is replaced" in src:
        print("  nightly2.py already patched")
        return
    if src.count(NIGHTLY_OLD) != 1:
        sys.exit("  nightly2.py not as expected — nothing written")
    open(path, "w").write(src.replace(NIGHTLY_OLD, NIGHTLY_NEW))
    print("  nightly2.py patched: the merge replaces every season it is given")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "build"):
        sys.exit(__doc__)
    import export_full as X
    env = X.load_env()
    con = X.connect(env)
    s3, bucket = X.s3c(env), env["R2_BUCKET_NAME"]
    seasons, ids = players_in_hot(con)
    print("  hot database seasons: %s · %d players have played in them" % (", ".join(seasons), len(ids)))
    if sys.argv[1] == "check":
        check(con, s3, bucket, ids, X)
        return
    patch_nightly()
    with tempfile.TemporaryDirectory() as tmp:
        for i, pid in enumerate(ids, 1):
            for kind, sql in (("gamelog", X.GAMELOG_SQL), ("shots", X.SHOTS_SQL),
                              ("assists", X.ASSISTS_SQL)):
                X.put(con, s3, bucket, sql % pid, "%s/players/%s/%s.parquet" % (X.PREFIX, pid, kind), tmp)
            if i % 100 == 0:
                print("    rebuilt %d / %d" % (i, len(ids)), flush=True)
    print("  rebuilt gamelog, shots and assists for %d players" % len(ids))
    check(con, s3, bucket, ids, X)


if __name__ == "__main__":
    main()
