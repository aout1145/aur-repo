#!/usr/bin/env python3
"""Upload the assembled repository to object storage (Cloudflare R2).

``rclone copy`` is used instead of ``rclone sync`` so that files which are not
part of this run (for example packages that were not rebuilt) are left intact.
Optionally, older package files that are no longer referenced by the database
are removed when ``repo.remove_old`` is enabled.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import load_config, load_db, log, rclone_available, run  # noqa: E402


def remote_list(remote: str) -> list[str]:
    proc = run(
        ["rclone", "lsf", "--files-only", remote],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.toml")
    parser.add_argument("--repo-dir", default="public")
    parser.add_argument("--remote", default=os.environ.get("R2_PATH", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo_name = cfg["repo"]["name"]

    if not args.remote:
        log("R2_PATH is not set; skipping upload")
        return 0
    if not rclone_available():
        log("R2 credentials are not configured; skipping upload")
        return 0

    log(f"uploading {args.repo_dir} -> {args.remote}")
    run(["rclone", "copy", args.repo_dir, args.remote], dry_run=args.dry_run)

    if cfg["repo"].get("remove_old"):
        prune(args.repo_dir, repo_name, args.remote, args.dry_run)

    return 0


def prune(repo_dir: str, repo_name: str, remote: str, dry_run: bool) -> None:
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    db = load_db(db_path)
    keep = {f"{repo_name}.db", f"{repo_name}.db.sig"}
    for entry in db.values():
        filename = entry.get("FILENAME")
        if filename:
            keep.add(filename)
            keep.add(filename + ".sig")

    removed = 0
    for name in remote_list(remote):
        if fnmatch.fnmatch(name, "*.pkg.tar.*") and name not in keep:
            log(f"removing obsolete {name}")
            run(["rclone", "deletefile", remote.rstrip("/") + "/" + name], dry_run=dry_run)
            removed += 1
    if removed:
        log(f"removed {removed} obsolete package file(s)")


if __name__ == "__main__":
    raise SystemExit(main())
