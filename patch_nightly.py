#!/usr/bin/env python3
"""
patch_nightly.py — make the nightly job do what the site now depends on.

Before this, the nightly run (GitHub Actions, running the scripts as committed)
would, on the first night with new games:

  - rewrite the current season's player_season.parquet without BPM
  - rebuild the registry from `players` alone, dropping everything player_bio
    fills in (birthdates, schools, heights, draft)
  - never write seasons/{season}/games.parquet, so new games are not
    clickable on the Scoreboard
  - never refresh the NBA.com schedule, rosters or preseason

And in GitHub Actions there is no .env file, so compute_bpm.py,
export_full.py and export_games_index.py would exit on "not found" anyway.

Four files change:

  nightly2.py                  rosters, schedule and (Sep-Oct) preseason
                               every night; BPM and the games index after
                               every export; registry built with player_bio
  compute_bpm.py               safe to re-run on a file that already has BPM
  export_full.py               `seasons` puts BPM back after rewriting, so a
                               manual export can no longer wipe it
  .github/workflows/nightly.yml  writes ~/boxandone/.env from the repository
                               secrets before the run

Every anchor is checked before anything is written. If one is missing, no
file is changed. Backups are written as *.bak_nightly.

    python3 patch_nightly.py
"""
import os
import shutil
import sys

HOME = os.path.expanduser("~/boxandone")

EDITS = {}

# ---------------------------------------------------------------- nightly2.py
EDITS["nightly2.py"] = [
(
"""        after = games_held(season)
        new_games = sorted(after - before)
        log("%d new games" % len(new_games))

        if new_games:
            ok &= run(log, ["bridge_games.py", "build",
                            "--seasons", "%s:%s" % (season, season)])
            if not a.no_pbp:
                ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season])
                ok &= run(log, ["ingest_pbp.py", "parse", "--season", season])
            ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])

            log("exporting season aggregates")
            export_season(env, log, season)

            pids = changed_players(season, new_games)
""",
"""        after = games_held(season)
        new_games = sorted(after - before)
        log("%d new games" % len(new_games))

        # NBA.com sources Basketball Reference does not carry. Rosters first,
        # so a rookie exists before his first preseason box score arrives.
        rows_before = box_rows(season)
        ok &= run(log, ["ingest_rosters.py", "build", "--season", season])
        ok &= run(log, ["ingest_schedule.py", "build"])
        if datetime.now().month in (9, 10):
            ok &= run(log, ["ingest_preseason.py", "build", "--season", season])
        pre_games = sorted(games_held(season) - after)
        grew = box_rows(season) > rows_before
        log("%d new preseason games" % len(pre_games))

        if new_games or pre_games or grew:
            if new_games:
                ok &= run(log, ["bridge_games.py", "build",
                                "--seasons", "%s:%s" % (season, season)])
                if not a.no_pbp:
                    ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season])
                    ok &= run(log, ["ingest_pbp.py", "parse", "--season", season])
                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])

            log("exporting season aggregates")
            export_season(env, log, season)
            # export_season rewrites player_season without BPM; put it back
            ok &= run(log, ["compute_bpm.py", "--season", season])
            ok &= run(log, ["export_games_index.py", "build", "--season", season])

            pids = changed_players(season, new_games + pre_games)
""", 1),
(
"""def changed_players(season, new_games):""",
"""def box_rows(season):
    \"\"\"Box-score rows held for a season. A preseason game first ingested with
    some players unknown gains rows later, once rosters add them, without
    becoming a new game; this is how that is noticed.\"\"\"
    import duckdb
    if not os.path.exists(DB_PATH):
        return 0
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        n = con.execute(\"\"\"SELECT COUNT(*) FROM player_game_box b
                          JOIN games g ON g.game_id = b.game_id
                          WHERE g.season = ?\"\"\", [season]).fetchone()[0]
    except Exception:
        n = 0
    con.close()
    return n


def changed_players(season, new_games):""", 1),
]

REG_START = '        reg = os.path.join(tmp, "registry.parquet")\n'
REG_END = '        s3.upload_file(reg, bucket, "%s/registry/players.parquet" % PREFIX)\n'
REG_NEW = '''        reg = os.path.join(tmp, "registry.parquet")
        # What a source claims about a player lives in player_bio, kept apart
        # from the registry so a scrape never overwrites a better-trusted
        # value. The registry wins; the scrape only fills blanks. Same rule
        # as export_full.REGISTRY_SQL.
        has_bio = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() "
            "WHERE database_name='hot' AND table_name='player_bio'").fetchone()[0] > 0
        if has_bio:
            cols = """
                COALESCE(p.birthdate, bio.birthdate) AS birthdate,
                CASE WHEN p.birthdate IS NULL AND bio.birthdate IS NOT NULL
                     THEN 'confirmed' ELSE p.birthdate_status END AS birthdate_status,
                p.position,
                COALESCE(p.height_in, bio.height_in) AS height_in,
                COALESCE(p.weight_lb, bio.weight_lb) AS weight_lb,
                COALESCE(p.college, bio.college) AS college,
                COALESCE(p.draft_year, bio.draft_year) AS draft_year,
                COALESCE(p.draft_round, bio.draft_round) AS draft_round,
                COALESCE(p.draft_pick, bio.draft_pick) AS draft_pick,
                p.status,
                COALESCE(p.nationality, bio.country) AS nationality"""
            join = ("LEFT JOIN hot.player_bio bio "
                    "ON bio.player_id=p.player_id AND bio.source='nba'")
        else:
            log("  player_bio not in this database — registry without enrichment")
            cols = """
                p.birthdate, p.birthdate_status, p.position,
                p.height_in, p.weight_lb, p.college, p.draft_year,
                p.draft_round, p.draft_pick, p.status, p.nationality"""
            join = ""
        sql = ("""
            COPY (
              SELECT p.player_id, p.full_name, p.display_name, p.name_normalized,
                     """ + cols + """,
                     i.source_id AS bbref_id, n.source_id AS nba_id,
                     s.seasons, s.first_season, s.last_season,
                     s.career_gp, s.career_pts
              FROM hot.players p
              LEFT JOIN hot.player_identifiers i
                ON i.player_id=p.player_id AND i.source='bbref'
              LEFT JOIN hot.player_identifiers n
                ON n.player_id=p.player_id AND n.source='nba'
              """ + join + """
              LEFT JOIN (
                SELECT player_id,
                       COUNT(DISTINCT season) AS seasons,
                       MIN(season) AS first_season, MAX(season) AS last_season,
                       SUM(gp) AS career_gp, SUM(pts) AS career_pts
                FROM read_parquet('%s', union_by_name=true)
                WHERE season_type='regular'
                GROUP BY 1
              ) s ON s.player_id = p.player_id
            ) TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        con.execute(sql % (glob, reg))
'''

# -------------------------------------------------------------- compute_bpm.py
EDITS["compute_bpm.py"] = [
(
"""            out = os.path.join(tmp, "out.parquet")
""",
"""            # a file that already carries BPM (a re-run) would otherwise come
            # out with every column twice; drop the old ones first
            have_cols = [d[0] for d in con.execute(
                "SELECT * FROM read_parquet('%s') LIMIT 0" % ps).description]
            stale = [c for c in ("bpm", "obpm", "dbpm", "vorp", "pos_est", "role_est")
                     if c in have_cols]
            if stale:
                ps2 = os.path.join(tmp, "ps_clean.parquet")
                con.execute("COPY (SELECT * EXCLUDE (%s) FROM read_parquet('%s')) "
                            "TO '%s' (FORMAT PARQUET)" % (", ".join(stale), ps, ps2))
                ps = ps2
            out = os.path.join(tmp, "out.parquet")
""", 1),
]

# --------------------------------------------------------------- export_full.py
EDITS["export_full.py"] = [
(
"""    print("\\n  %d seasons, %.1f MB" % (len(seasons), tot / 1048576.0))
""",
"""    print("\\n  %d seasons, %.1f MB" % (len(seasons), tot / 1048576.0))
    # player_season was just rewritten without BPM; compute_bpm adds it back
    print("\\n  restoring BPM")
    sys.path.insert(0, HOME)
    import compute_bpm
    for s in seasons:
        compute_bpm.run(env, only=s)
""", 1),
]

# ----------------------------------------------------- .github/workflows/nightly.yml
EDITS[".github/workflows/nightly.yml"] = [
(
"""      - name: Run
""",
"""      # Several scripts read credentials only from ~/boxandone/.env. Actions
      # has no such file, so write one from the repository secrets. It lives
      # on the runner for the length of the job and is never committed.
      - name: Write credentials file
        env:
          R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          R2_ENDPOINT_URL: ${{ secrets.R2_ENDPOINT_URL }}
          R2_BUCKET_NAME: ${{ secrets.R2_BUCKET_NAME }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_ANON_KEY: ${{ secrets.SUPABASE_ANON_KEY }}
        run: |
          mkdir -p ~/boxandone
          umask 077
          for k in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT_URL \\
                   R2_BUCKET_NAME SUPABASE_URL SUPABASE_ANON_KEY; do
            echo "$k=${!k}"
          done > ~/boxandone/.env

      - name: Run
""", 1),
]


def main():
    srcs, problems = {}, []
    for rel, edits in EDITS.items():
        path = os.path.join(HOME, rel)
        if not os.path.exists(path):
            problems.append("  %s not found" % rel)
            continue
        src = open(path, encoding="utf-8").read()
        if "patch_nightly" in src or "Write credentials file" in src \
                or "restoring BPM" in src or "ps_clean.parquet" in src:
            problems.append("  %s already patched" % rel)
            continue
        for old, _, want in edits:
            n = src.count(old)
            if n != want:
                problems.append("  %s: anchor found %d time(s), expected %d:\n      %s"
                                % (rel, n, want, old.strip().splitlines()[0]))
        if rel == "nightly2.py":
            if src.count(REG_START) != 1 or src.count(REG_END) != 1:
                problems.append("  nightly2.py: registry block anchors not unique")
        srcs[rel] = src
    if problems:
        print("  NOT PATCHED — nothing was written:")
        print("\n".join(problems))
        sys.exit(1)

    for rel, src in srcs.items():
        for old, new, _ in EDITS[rel]:
            src = src.replace(old, new)
        if rel == "nightly2.py":
            a = src.index(REG_START)
            b = src.index(REG_END)
            src = src[:a] + REG_NEW + src[b:]
            src = src.replace("import subprocess\n",
                              "import subprocess  # patched by patch_nightly\n", 1)
        path = os.path.join(HOME, rel)
        shutil.copy(path, path + ".bak_nightly")
        open(path, "w", encoding="utf-8").write(src)
        print("  patched %s" % rel)
    print("\n  Backups: *.bak_nightly. Review with:  git diff")


if __name__ == "__main__":
    main()
