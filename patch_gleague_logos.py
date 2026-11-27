#!/usr/bin/env python3
"""
patch_gleague_logos.py — run once.

The G League's own site serves every team's logo from
ak-static.cms.nba.com/wp-content/uploads/logos/nbagleague/{team_id}/primary/{L|D}/logo.svg
(L is the version for light backgrounds, which is what our pages use; D for
dark). Those two go to the front of the patterns the logo step tries. Logos
already on file are left as they are; the next `teams` run fetches the rest.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_gleague.py")
src = open(PATH).read()
if "uploads/logos/nbagleague" in src:
    print("  ingest_gleague.py already patched")
    sys.exit()
OLD = '''LOGO_URLS = [
'''
NEW = '''LOGO_URLS = [
    "https://ak-static.cms.nba.com/wp-content/uploads/logos/nbagleague/{tid}/primary/L/logo.svg",
    "https://ak-static.cms.nba.com/wp-content/uploads/logos/nbagleague/{tid}/primary/D/logo.svg",
'''
if src.count(OLD) != 1:
    sys.exit("  ingest_gleague.py not as expected — nothing written")
open(PATH, "w").write(src.replace(OLD, NEW))
print("  ingest_gleague.py patched: logos from the G League site's own address")
