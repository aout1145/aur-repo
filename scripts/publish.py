#!/usr/bin/env python3
"""Publish the assembled repository to a GitHub release.

All files are uploaded as release assets of a single, fixed release (the tag
comes from ``[repo].tag`` in ``packages.toml``).  This means pacman can use the
release download directory directly as its ``Server``:

    https://github.com/<owner>/<repo>/releases/download/<tag>

or, when fronted by the bundled Cloudflare Worker, a single custom domain.

The update is done in a safe order:

  1. every package file (and ``repo.gpg``) is uploaded first;
  2. the database (``<name>.db`` and its signature) is replaced last, and the
     previous database is restored if that replacement fails.

A build failure never reaches this script, so a failed build leaves the release
untouched.  Even if this script fails part-way, the database still points at
packages that are actually present, so clients stay consistent.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import (  # noqa: E402
    gh_available,
    gh_ensure_release,
    github_repo,
    load_config,
    load_db,
    log,
    run,
)

BATCH_SIZE = 40


def write_manifest(repo_dir: str, repo_name: str) -> str:
    """Write ``packages.json`` (used by the Worker homepage) from the database."""
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    db = load_db(db_path)
    packages = sorted(
        (
            {
                "name": name,
                "version": entry.get("VERSION", ""),
                "filename": entry.get("FILENAME", ""),
                "desc": entry.get("DESC", ""),
                "arch": entry.get("ARCH", ""),
            }
            for name, entry in db.items()
            if entry.get("FILENAME")
        ),
        key=lambda p: p["name"],
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signed": bool(os.environ.get("GPG_KEY")),
        "key_id": os.environ.get("GPG_KEY", ""),
        "packages": packages,
    }
    path = os.path.join(repo_dir, "packages.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def upload(repo: str, tag: str, files: list[str], dry_run: bool) -> None:
    files = [f for f in files if os.path.exists(f)]
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i : i + BATCH_SIZE]
        run(
            ["gh", "release", "upload", tag, *batch, "--repo", repo, "--clobber"],
            dry_run=dry_run,
        )


def download_asset(repo: str, tag: str, name: str, directory: str) -> bool:
    proc = run(
        ["gh", "release", "download", tag, "--repo", repo, "--pattern", name,
         "--dir", directory, "--clobber"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and os.path.exists(os.path.join(directory, name))


def replace_database(repo: str, tag: str, db_files: list[str], dry_run: bool) -> None:
    """Replace the published database, restoring the old one on failure."""
    if not db_files:
        return
    with tempfile.TemporaryDirectory() as tmp:
        backups: list[str] = []
        for path in db_files:
            name = os.path.basename(path)
            if download_asset(repo, tag, name, tmp):
                backups.append(os.path.join(tmp, name))
        try:
            upload(repo, tag, db_files, dry_run)
        except subprocess.CalledProcessError:
            log("error: database upload failed; restoring the previous database")
            if backups:
                try:
                    upload(repo, tag, backups, dry_run)
                except subprocess.CalledProcessError:
                    log("error: could not restore the previous database")
            raise


def release_id(repo: str, tag: str) -> str:
    proc = run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".id"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def release_assets(repo: str, tag: str) -> list[tuple[str, str]]:
    """Return ``(asset_id, name)`` pairs for the release.

    Uses the paginated assets endpoint so it also works for releases with many
    assets.
    """
    rid = release_id(repo, tag)
    if not rid:
        return []
    proc = run(
        ["gh", "api", "--paginate", f"repos/{repo}/releases/{rid}/assets",
         "--jq", r'.[] | "\(.id) \(.name)"'],
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


def verify_uploaded(repo: str, tag: str, names: list[str]) -> None:
    """Fail if any expected asset is not actually present in the release."""
    present = {name for _, name in release_assets(repo, tag)}
    missing = sorted(set(names) - present)
    if missing:
        raise SystemExit(
            "upload verification failed; missing release assets: " + ", ".join(missing)
        )
    log(f"upload verified ({len(names)} asset(s))")


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

    if not args.repo or not gh_available() or not os.path.isdir(args.repo_dir):
        if not args.repo:
            problem = "no repository configured (set GITHUB_REPOSITORY or pass --repo)"
        elif not gh_available():
            problem = "the 'gh' CLI is not installed"
        else:
            problem = f"{args.repo_dir} does not exist; did the build step produce artifacts?"
        if args.dry_run:
            log(f"dry run: {problem}; skipping")
            return 0
        raise SystemExit(problem)

    gh_ensure_release(args.repo, tag, args.dry_run)

    # Regenerate the manifest used by the Worker homepage from the database we
    # are about to publish, so it is uploaded together with the packages.
    write_manifest(args.repo_dir, repo_name)

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

    if not db_files:
        if args.dry_run:
            log("dry run: no database to publish")
            return 0
        raise SystemExit(
            f"refusing to publish: {repo_name}.db was not found in {args.repo_dir}"
        )

    # 1) Packages and the public key first, 2) the database last (atomically).
    upload(args.repo, tag, payload, args.dry_run)
    replace_database(args.repo, tag, db_files, args.dry_run)

    # Never report success unless the assets really are present in the release.
    if not args.dry_run:
        verify_uploaded(args.repo, tag, [os.path.basename(f) for f in [*payload, *db_files]])

    if cfg["repo"].get("remove_old"):
        prune(args.repo, tag, args.repo_dir, repo_name, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
