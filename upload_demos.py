"""Upload demos from this machine into the bucket the analyzer reads.

FACEIT's Downloads API is the only way to fetch demos from the Data API's private
resource URLs, and non-commercial applications for it are paused - so demos reach the
bucket by being uploaded. Premier demos always needed this route anyway: CS2 writes
them to disk locally.

Scans the configured folders, compresses anything still raw, skips what the bucket
already has, and uploads the rest. Safe to run repeatedly - it is how the scheduled
task uses it.

  set DEMO_DIRS=C:\\Users\\emanu\\Downloads;C:\\...\\csgo\\replays
  set R2_BUCKET / R2_ENDPOINT / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
  python upload_demos.py [--dry-run] [--delete-after]
"""
import argparse
import os
import pathlib
import sys

import boto3
import botocore
import zstandard

BUCKET = os.environ.get("R2_BUCKET", "cs2-demos")
ENDPOINT = os.environ.get("R2_ENDPOINT", "")
DEMO_PREFIX = "demos/"
SUFFIXES = (".dem", ".dem.zst", ".dem.bz2")
COMPRESS_LEVEL = 10          # ~29% smaller; the demo is already packed binary
DEFAULT_DIRS = [
    pathlib.Path.home() / "Downloads",
    pathlib.Path(r"C:\Program Files (x86)\Steam\steamapps\common"
                 r"\Counter-Strike Global Offensive\game\csgo\replays"),
]


def demo_dirs():
    raw = os.environ.get("DEMO_DIRS", "")
    if raw:
        return [pathlib.Path(p) for p in raw.split(os.pathsep) if p]
    return DEFAULT_DIRS


def client():
    if not ENDPOINT:
        sys.exit("R2_ENDPOINT is not set")
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="auto")


def find_demos(dirs):
    """Every demo in the given folders, newest first, de-duplicated by name.

    A .dem next to its own .dem.zst is the unpacked copy of it - upload one, not both.
    """
    found = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for path in d.iterdir():
            name = path.name
            if not name.endswith(SUFFIXES) or not path.is_file():
                continue
            stem = name[:-4] if name.endswith((".zst", ".bz2")) else name
            # Prefer the compressed copy: it is what we would upload anyway.
            if stem in found and found[stem].name.endswith((".zst", ".bz2")):
                continue
            found[stem] = path
    return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def key_for(path):
    name = path.name if path.name.endswith((".zst", ".bz2")) else path.name + ".zst"
    return DEMO_PREFIX + name


def exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def compress(path, out_dir):
    """Raw .dem -> .dem.zst. Demos are ~285 MB raw and ~205 MB compressed."""
    dest = out_dir / (path.name + ".zst")
    cctx = zstandard.ZstdCompressor(level=COMPRESS_LEVEL)
    with path.open("rb") as src, dest.open("wb") as dst:
        cctx.copy_stream(src, dst)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="list what would upload")
    ap.add_argument("--delete-after", action="store_true",
                    help="remove the local demo once it is in the bucket")
    args = ap.parse_args()

    dirs = demo_dirs()
    demos = find_demos(dirs)
    print("scanning: %s" % ", ".join(str(d) for d in dirs))
    print("found %d demo(s)" % len(demos))
    if not demos:
        return 0

    s3 = None if args.dry_run else client()
    scratch = pathlib.Path(os.environ.get("TEMP", ".")) / "cs2-upload"
    scratch.mkdir(parents=True, exist_ok=True)
    uploaded = 0

    for path in demos:
        key = key_for(path)
        if args.dry_run:
            print("would upload %-60s -> %s" % (path.name, key))
            continue
        if exists(s3, key):
            print("skip (already in bucket) %s" % path.name)
            continue

        temp = None
        try:
            source = path
            if path.suffix == ".dem":
                print("compressing %s ..." % path.name)
                temp = source = compress(path, scratch)
            size = source.stat().st_size / 1e6
            print("uploading %s (%.0f MB) -> %s" % (path.name, size, key))
            s3.upload_file(str(source), BUCKET, key)
            uploaded += 1
            if args.delete_after:
                path.unlink()
                print("  removed local copy")
        except Exception as e:
            print("  FAILED: %r" % (e,))
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    print("uploaded %d demo(s); the analyzer parses them on its next run" % uploaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
