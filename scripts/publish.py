#!/usr/bin/env python3
"""Publish the assembled repository to a GitHub release.

All files are uploaded as release assets of a single, fixed release (the tag
comes from ``[repo].tag`` in ``packages.toml``).  This means pacman can use the
release download directory directly as its ``Server``:

    https://github.com/<owner>/<repo>/releases/download/<tag>

or, when fronted by the bundled Cloudflare Worker, a single custom domain.

Package files are uploaded before the database so that a failed upload cannot
leave a database that references packages that were never uploaded.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import (  # noqa: E402
    gh_available,
    github_repo,
    load_config,
    load_db,
    log,
    run,
)

BATCH_SIZE = 40


def ensure_release(repo: str, tag: str, dry_run: bool) -> None:
    proc = run(
        ["gh", "release", "view", tag, "--repo", repo],
        check=False,
        capture_output=True,
        text=True,
        dry_run=dry_run,
    )
    if proc.returncode == 0:
        return
    log(f"creating release '{tag}'")
    run(
        [
            "gh", "release", "create", tag,
            "--repo", repo,
            "--title", f"AUR repository ({tag})",
            "--notes", "Binary packages published by the AUR build workflow.",
            "--latest=false",
        ],
        dry_run=dry_run,
    )


def upload(repo: str, tag: str, files: list[str], dry_run: bool) -> None:
    files = [f for f in files if os.path.exists(f)]
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i : i + BATCH_SIZE]
        run(
            ["gh", "release", "upload", tag, *batch, "--repo", repo, "--clobber"],
            dry_run=dry_run,
        )


def release_assets(repo: str, tag: str) -> list[tuple[str, str]]:
    """Return ``(asset_id, name)`` pairs for the release."""
    proc = run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq",
         r'.assets[] | "\(.id) \(.name)"'],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    assets = []
    for line in proc.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            assets.append((parts[0], parts[1]))
    return assets


def prune(repo: str, tag: str, repo_dir: str, repo_name: str, dry_run: bool) -> None:
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    db = load_db(db_path)
    keep = {f"{repo_name}.db", f"{repo_name}.db.sig", "repo.gpg"}
    for entry in db.values():
        filename = entry.get("FILENAME")
        if filename:
            keep.add(filename)
            keep.add(filename + ".sig")

    removed = 0
    for asset_id, name in release_assets(repo, tag):
        if fnmatch.fnmatch(name, "*.pkg.tar.*") and name not in keep:
            log(f"removing obsolete asset {name}")
            run(
                ["gh", "api", "-X", "DELETE", f"repos/{repo}/releases/assets/{asset_id}"],
                dry_run=dry_run,
            )
            removed += 1
    if removed:
        log(f"removed {removed} obsolete asset(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.toml")
    parser.add_argument("--repo-dir", default="public")
    parser.add_argument("--repo", default=github_repo(), help="owner/repo (defaults to $GITHUB_REPOSITORY)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo_name = cfg["repo"]["name"]
    tag = cfg["repo"].get("tag", "repo")

    if not args.repo:
        log("no repository configured (set GITHUB_REPOSITORY or pass --repo); skipping upload")
        return 0
    if not gh_available():
        log("the 'gh' CLI is not installed; skipping upload")
        return 0
    if not os.path.isdir(args.repo_dir):
        log(f"{args.repo_dir} does not exist; nothing to upload")
        return 0

    ensure_release(args.repo, tag, args.dry_run)

    # Everything except repo-add's working files (.db.tar.gz / .files*);
    # only the `<name>.db` and its signature are served.
    working = {
        f"{repo_name}.db.tar.gz",
        f"{repo_name}.db.tar.gz.sig",
        f"{repo_name}.files.tar.gz",
        f"{repo_name}.files",
        f"{repo_name}.files.sig",
        f"{repo_name}.files.tar.gz.sig",
    }
    all_files = sorted(
        os.path.join(args.repo_dir, f)
        for f in os.listdir(args.repo_dir)
        if f not in working
    )
    db_files = [f for f in all_files if os.path.basename(f) in (f"{repo_name}.db", f"{repo_name}.db.sig")]
    payload = [f for f in all_files if f not in db_files]

    # Packages and the public key first, the database last.
    upload(args.repo, tag, payload, args.dry_run)
    upload(args.repo, tag, db_files, args.dry_run)

    if cfg["repo"].get("remove_old"):
        prune(args.repo, tag, args.repo_dir, repo_name, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
