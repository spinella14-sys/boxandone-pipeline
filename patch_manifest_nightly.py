#!/usr/bin/env python3
"""
patch_manifest_nightly.py — run once.

The manifest's written_at changes whenever the registry is rebuilt, and the
hourly transactions job rebuilds it too (any hour with a signing or a trade).
So the dashboard's "the nightly update last finished …" was often reporting an
hourly run: 6:43 AM is the :37 job plus six minutes.

The manifest now carries nightly_at, written only by the nightly job and kept
as it was when the hourly job rewrites the manifest. The dashboard reads that.
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "nightly2.py")
src = open(PATH).read()
if "nightly_at" in src:
    print("  nightly2.py already patched")
    sys.exit()

PAIRS = [
    ('def rebuild_registry_and_manifest(env, log):',
     'def rebuild_registry_and_manifest(env, log, nightly=False):'),
    ('''        man = {
            "written_at": datetime.now(timezone.utc).isoformat(),''',
     '''        # when the nightly job last finished: set by the nightly only, carried
        # over unchanged when the hourly job rewrites the manifest
        prev_nightly = None
        try:
            prev = json.loads(s3.get_object(Bucket=bucket, Key="%s/manifest.json" % PREFIX)["Body"].read())
            prev_nightly = prev.get("nightly_at")
        except Exception:
            pass
        man = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "nightly_at": datetime.now(timezone.utc).isoformat() if nightly else prev_nightly,'''),
    ('            rebuild_registry_and_manifest(env, log)\n',
     '            rebuild_registry_and_manifest(env, log, nightly=True)\n'),
]
for old, new in PAIRS:
    if src.count(old) != 1:
        sys.exit("  nightly2.py not as expected (%r) — nothing written" % old[:60])
    src = src.replace(old, new)
open(PATH, "w").write(src)
print("  nightly2.py patched: manifest carries nightly_at")
