#!/usr/bin/env python3
"""
patch_cdn.py — run once. Switches the nightly play-by-play to NBA's CDN feed.

  ingest_pbp.py      fetch --source cdn   CDN play-by-play + box score, translated
  bridge_games.py    build --source schedule   NBA game ids from the CDN schedule
  bridge_nba_ids.py  build --source cdn   NBA player ids from CDN box scores
  nightly2.py        uses all three for the current season, and bridges player
                     ids BEFORE parsing, so a rookie's events resolve the same
                     night instead of never

Without --source each script behaves exactly as before (stats.nba.com), so
older seasons and backfills are unchanged. Needs cdn_feed.py in ~/boxandone.
"""
import os
import sys

HOME = os.path.expanduser("~/boxandone")


def edit(name, pairs):
    path = os.path.join(HOME, name)
    src = open(path).read()
    if "--source" in src and "cdn_feed" in src:
        print("  %-18s already patched" % name)
        return
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("  %s: expected text not found once — nothing written to it:\n%s"
                     % (name, old[:200]))
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %-18s patched" % name)


if not os.path.exists(os.path.join(HOME, "cdn_feed.py")):
    sys.exit("  cdn_feed.py must be in ~/boxandone first")

# ---------------------------------------------------------------- ingest_pbp
V3_BLOCK = '''                url = ("https://stats.nba.com/stats/playbyplayv3"
                       "?GameID=%s&StartPeriod=0&EndPeriod=14" % nba_gid)
                got = None
                for attempt in range(4):
                    try:
                        r = cr.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=60)
                    except Exception as e:
                        print("    net %s: %s" % (nba_gid, str(e)[:50]))
                        time.sleep(15 * (attempt + 1))
                        continue
                    if r.status_code == 200:
                        got = r.text
                        break
                    print("    HTTP %s on %s" % (r.status_code, nba_gid))
                    time.sleep(20 * (attempt + 1))
'''
CDN_BLOCK = '''                if source == "cdn":
                    # NBA's CDN, translated to playbyplayv3's shape (cdn_feed.py)
                    import cdn_feed
                    got = cdn_feed.fetch_translated(nba_gid)
                else:
''' + "\n".join(("    " + l) if l else l for l in V3_BLOCK.split("\n"))

edit("ingest_pbp.py", [
    ("def cmd_fetch(season, limit=None):", 'def cmd_fetch(season, limit=None, source="nba"):'),
    (V3_BLOCK, CDN_BLOCK.rstrip(" ")),
    ("                time.sleep(DELAY + random.uniform(0, JITTER))",
     "                time.sleep(0.3 if source == \"cdn\" else DELAY + random.uniform(0, JITTER))"),
    ('    ap.add_argument("--limit", type=int)\n',
     '    ap.add_argument("--limit", type=int)\n'
     '    ap.add_argument("--source", choices=["nba", "cdn"], default="nba")\n'),
    ("        cmd_fetch(a.season, a.limit)", "        cmd_fetch(a.season, a.limit, a.source)"),
])

# -------------------------------------------------------------- bridge_games
edit("bridge_games.py", [
    ('    ap.add_argument("--force", action="store_true")\n    a = ap.parse_args()\n',
     '    ap.add_argument("--force", action="store_true")\n'
     '    ap.add_argument("--source", choices=["nba", "schedule"], default="nba")\n'
     '    a = ap.parse_args()\n'
     '    if a.source == "schedule":\n'
     '        # NBA game ids from the CDN schedule instead of stats.nba.com (cdn_feed.py)\n'
     '        import cdn_feed\n'
     '        cdn_feed.preload("schedule")\n'
     '        fetch_log = cdn_feed.schedule_log\n'),
])

# ------------------------------------------------------------ bridge_nba_ids
edit("bridge_nba_ids.py", [
    ('                    help="range, newest first, e.g. 2024-25:1996-97")\n    a = ap.parse_args()\n',
     '                    help="range, newest first, e.g. 2024-25:1996-97")\n'
     '    ap.add_argument("--source", choices=["nba", "cdn"], default="nba")\n'
     '    a = ap.parse_args()\n'
     '    if a.source == "cdn":\n'
     '        # player lines from CDN box scores instead of stats.nba.com (cdn_feed.py)\n'
     '        import cdn_feed\n'
     '        cdn_feed.preload("cdn")\n'
     '        fetch_gamelog = cdn_feed.player_log\n'),
])

# ------------------------------------------------------------------ nightly2
OLD_NIGHTLY = '''                ok &= run(log, ["bridge_games.py", "build",
                                "--seasons", "%s:%s" % (season, season)])
                if not a.no_pbp:
                    ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season])
                    ok &= run(log, ["ingest_pbp.py", "parse", "--season", season])
                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season])
'''
NEW_NIGHTLY = '''                # NBA's CDN rather than stats.nba.com, which GitHub cannot
                # reach. Player ids are bridged from its box scores BEFORE the
                # parse, so a rookie's events resolve the night he debuts.
                ok &= run(log, ["bridge_games.py", "build", "--source", "schedule",
                                "--seasons", "%s:%s" % (season, season)])
                if not a.no_pbp:
                    ok &= run(log, ["ingest_pbp.py", "fetch", "--season", season,
                                    "--source", "cdn"])
                ok &= run(log, ["bridge_nba_ids.py", "build", "--season", season,
                                "--source", "cdn"])
                if not a.no_pbp:
                    ok &= run(log, ["ingest_pbp.py", "parse", "--season", season,
                                    "--source", "cdn"])
'''
OLD_GATE = '''    if argv and argv[0] in STATS_STEPS and not stats_reachable(log):'''
NEW_GATE = '''    if argv and argv[0] in STATS_STEPS and "--source" not in argv \\
            and not stats_reachable(log):'''
OLD_SET = '''               "ingest_pbp.py", "bridge_nba_ids.py", "parse_assists.py",
               "export_game_files.py", "ingest_tracking.py"}'''
NEW_SET = '''               "ingest_pbp.py", "bridge_nba_ids.py", "ingest_tracking.py"}
# (a step given --source reads the CDN instead and always runs)'''

path = os.path.join(HOME, "nightly2.py")
src = open(path).read()
if '"--source", "cdn"' in src:
    print("  %-18s already patched" % "nightly2.py")
else:
    if "STATS_STEPS" not in src:
        sys.exit("  nightly2.py: run patch_stats_gate.py first")
    for old, new in ((OLD_NIGHTLY, NEW_NIGHTLY), (OLD_GATE, NEW_GATE), (OLD_SET, NEW_SET)):
        if src.count(old) != 1:
            sys.exit("  nightly2.py: expected text not found — nothing written:\n%s" % old[:200])
        src = src.replace(old, new)
    open(path, "w").write(src)
    print("  %-18s patched" % "nightly2.py")
