#!/usr/bin/env python3
"""
patch_r2_upload.py — run once. Makes the 166 MB database upload survive a
home connection.

The push failed with a read timeout on part 9: boto sends ten 8 MB parts at
once with a 60-second timeout each, which a home upload link cannot finish in
time. Now: four parts at a time, a 5-minute timeout, and ten retries that
back off, for every R2 transfer the jobs make. GitHub's runners are unaffected
in practice, just slightly more patient.
"""
import os
import sys

path = os.path.join(os.path.expanduser("~/boxandone"), "nightly2.py")
src = open(path).read()
if "read_timeout=300" in src:
    print("already patched")
    sys.exit(0)
pairs = [
    ('''                        config=Config(max_pool_connections=24,
                                      retries={"max_attempts": 3}))''',
     '''                        config=Config(max_pool_connections=24,
                                      connect_timeout=30, read_timeout=300,
                                      retries={"max_attempts": 10, "mode": "adaptive"}))'''),
    ('''    s3.upload_file(DB_PATH, bucket, DB_KEY)''',
     '''    from boto3.s3.transfer import TransferConfig
    # four 16 MB parts at a time: a home upload link cannot finish ten at
    # once inside the timeout, which is how a push from the Mac failed
    s3.upload_file(DB_PATH, bucket, DB_KEY, Config=TransferConfig(
        multipart_chunksize=16 * 1024 * 1024, max_concurrency=4))'''),
]
for old, new in pairs:
    if src.count(old) != 1:
        sys.exit("expected text not found — nothing written")
    src = src.replace(old, new)
open(path, "w").write(src)
print("nightly2.py patched — R2 uploads: 4 parts at a time, 5-minute timeout, 10 retries")
