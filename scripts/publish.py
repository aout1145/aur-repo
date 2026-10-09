#!/usr/bin/env python3
"""Publish the successfully built packages to a GitHub release.

``build.py`` writes ``result.json`` describing which packages failed, which
were locked (see build.py) and which may be published.  This script:

  1. uploads the package files (and ``repo.gpg``) first;
  2. replaces the database last, restoring the previous one on failure;
  3. writes ``repo.json`` (package list + per-package status) for the homepage;
  4. removes the obsolete ``packages.json`` / ``status.json`` assets;
  5. exits non-zero if any package failed, so the run is marked red even
     though everything that could be published was published.

Locked packages keep their previous files and database entries untouched.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
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
LEGACY_ASSETS = ("packages.json", "status.json")
RESULT_FILE = "result.json"
MANIFEST_FILE = "repo.json"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# GitHub release helpers
# ---------------------------------------------------------------------------
def upload(repo: str, tag: str, files: list[str], dry_run: bool) -> None:
    files = [f for f in files if os.path.exists(f)]
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i : i + BATCH_SIZE]
        run(
            ["gh", "release", "upload", tag, *batch, "--repo", repo, "--clobber"],
            dry_run=dry_run,
        )


def download_asset(repo: str, tag: str, name: str, directory: str) -> str | None:
    proc = run(
        ["gh", "release", "download", tag, "--repo", repo, "--pattern", name,
         "--dir", directory, "--clobber"],
        check=False,
        capture_output=True,
        text=True,
    )
    path = os.path.join(directory, name)
    return path if proc.returncode == 0 and os.path.exists(path) else None


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
    """Return ``(asset_id, name)`` pairs, via the paginated assets endpoint."""
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


def delete_assets(repo: str, tag: str, names: set[str], dry_run: bool) -> None:
    for asset_id, name in release_assets(repo, tag):
        if name in names:
            log(f"removing obsolete asset {name}")
            run(
                ["gh", "api", "-X", "DELETE", f"repos/{repo}/releases/assets/{asset_id}"],
                dry_run=dry_run,
            )


def verify_uploaded(repo: str, tag: str, names: list[str]) -> None:
    """Fail if any expected asset is not actually present in the release."""
    present = {name for _, name in release_assets(repo, tag)}
    missing = sorted(set(names) - present)
    if missing:
        raise SystemExit(
            "upload verification failed; missing release assets: " + ", ".join(missing)
        )
    log(f"upload verified ({len(names)} asset(s))")


# ---------------------------------------------------------------------------
# Manifest (homepage data)
# ---------------------------------------------------------------------------
def build_manifest(repo_dir: str, repo_name: str, result: dict, previous: dict) -> dict:
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    db = load_db(db_path)

    failed = set(result.get("failed_names", []))
    blocked = set(result.get("blocked_names", []))
    published = set(result.get("published_names", []))
    previous_by_name = {
        entry.get("name"): entry
        for entry in (previous or {}).get("packages", [])
        if entry.get("name")
    }

    now = utcnow()
    packages: list[dict] = []
    for name, entry in db.items():
        filename = entry.get("FILENAME")
        if not filename:
            continue
        if name in failed:
            status = "failed"
        elif name in blocked:
            status = "blocked"
        else:
            status = "ok"
        prev = previous_by_name.get(name)
        if name in published:
            updated_at = now
        elif prev and prev.get("updated_at"):
            updated_at = prev["updated_at"]
        else:
            updated_at = now
        packages.append({
            "name": name,
            "version": entry.get("VERSION", ""),
            "filename": filename,
            "updated_at": updated_at,
            "status": status,
        })

    # Packages that failed/were locked before ever being published have no file
    # in the database, so add them from the build result.
    known = {p["name"] for p in packages}
    for entry in result.get("new_entries", []):
        if entry.get("name") in known:
            continue
        packages.append({
            "name": entry.get("name", ""),
            "version": entry.get("version", ""),
            "filename": None,
            "updated_at": None,
            "status": entry.get("status", "failed"),
        })

    packages.sort(key=lambda p: (p.get("filename") or p.get("name") or ""))
    return {
        "repo": repo_name,
        "signed": bool(os.environ.get("GPG_KEY")),
        "key_id": os.environ.get("GPG_KEY", ""),
        "generated_at": now,
        "packages": packages,
    }


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------
def prune(repo: str, tag: str, repo_dir: str, repo_name: str, dry_run: bool) -> None:
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    db = load_db(db_path)
    keep = {f"{repo_name}.db", f"{repo_name}.db.sig", "repo.gpg", MANIFEST_FILE}
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


# ---------------------------------------------------------------------------
# Cloudflare cache purge
# ---------------------------------------------------------------------------
def obsolete_filenames(previous: dict, repo_dir: str, repo_name: str) -> list[str]:
    """Files present in the previous manifest but no longer in the database."""
    db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")
    if not os.path.exists(db_path):
        db_path = os.path.join(repo_dir, f"{repo_name}.db")
    current = {
        entry.get("FILENAME")
        for entry in load_db(db_path).values()
        if entry.get("FILENAME")
    }
    previous_files = {
        p.get("filename") for p in previous.get("packages", []) if p.get("filename")
    }
    return sorted(previous_files - current)


def cloudflare_purge_enabled() -> bool:
    """Whether `[deploy] purge` is enabled in cloudflare/wrangler.toml."""
    try:
        import tomllib

        with open("cloudflare/wrangler.toml", "rb") as fh:
            return bool(tomllib.load(fh).get("deploy", {}).get("purge", False))
    except (OSError, ValueError):
        return False


def purge_cloudflare_tags(tags: list[str], dry_run: bool) -> None:
    """Purge the given Cache-Tags from the Cloudflare cache (max 30 per call)."""
    if not tags:
        return
    zone = os.environ.get("CLOUDFLARE_ZONE_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not zone or not token:
        log("warning: cache purge enabled but CLOUDFLARE_ZONE_ID/CLOUDFLARE_API_TOKEN are not set")
        return

    url = f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache"
    for i in range(0, len(tags), 30):
        batch = tags[i : i + 30]
        log(f"purging {len(batch)} cache tag(s)")
        if dry_run:
            continue
        body = json.dumps({"tags": batch}).encode()
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            if not result.get("success"):
                log(f"warning: cache purge failed: {result.get('errors')}")
        except Exception as exc:  # noqa: BLE001 - purge is best effort
            log(f"warning: cache purge request failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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

    result_path = os.path.join(args.repo_dir, RESULT_FILE)
    result = {}
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
    failed_any = bool(result.get("failed_any"))

    if not args.repo or not gh_available() or not os.path.isdir(args.repo_dir):
        if not args.repo:
            problem = "no repository configured (set GITHUB_REPOSITORY or pass --repo)"
        elif not gh_available():
            problem = "the 'gh' CLI is not installed"
        else:
            problem = f"{args.repo_dir} does not exist; did the build step produce artifacts?"
        if args.dry_run:
            log(f"dry run: {problem}; skipping")
            return 5 if failed_any else 0
        raise SystemExit(problem)

    gh_ensure_release(args.repo, tag, args.dry_run)

    # Merge the previous manifest so unchanged packages keep their timestamps.
    with tempfile.TemporaryDirectory() as tmp:
        previous_path = download_asset(args.repo, tag, MANIFEST_FILE, tmp)
        previous = {}
        if previous_path:
            try:
                with open(previous_path, encoding="utf-8") as fh:
                    previous = json.load(fh)
            except (OSError, json.JSONDecodeError):
                previous = {}

    manifest = build_manifest(args.repo_dir, repo_name, result, previous)
    manifest_path = os.path.join(args.repo_dir, MANIFEST_FILE)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # Only the database, its signature and the manifest are served; everything
    # else (repo-add's working files, result.json) is internal.
    working = {
        f"{repo_name}.db.tar.gz",
        f"{repo_name}.db.tar.gz.sig",
        f"{repo_name}.files.tar.gz",
        f"{repo_name}.files",
        f"{repo_name}.files.sig",
        f"{repo_name}.files.tar.gz.sig",
        RESULT_FILE,
    }
    all_files = sorted(
        os.path.join(args.repo_dir, f)
        for f in os.listdir(args.repo_dir)
        if f not in working
    )
    db_files = [f for f in all_files if os.path.basename(f) in (f"{repo_name}.db", f"{repo_name}.db.sig")]
    payload = [
        f for f in all_files
        if f not in db_files and os.path.basename(f) != MANIFEST_FILE
    ]

    expected = [os.path.basename(f) for f in payload]
    if db_files:
        expected += [os.path.basename(f) for f in db_files]
    expected.append(MANIFEST_FILE)

    # 1) packages and public key, 2) database, 3) the manifest.
    upload(args.repo, tag, payload, args.dry_run)
    replace_database(args.repo, tag, db_files, args.dry_run)
    upload(args.repo, tag, [manifest_path], args.dry_run)

    delete_assets(args.repo, tag, set(LEGACY_ASSETS), args.dry_run)

    if not args.dry_run:
        verify_uploaded(args.repo, tag, expected)

    if cfg["repo"].get("remove_old"):
        prune(args.repo, tag, args.repo_dir, repo_name, args.dry_run)

    # Purge replaced/removed package files from the Cloudflare edge cache.
    if cloudflare_purge_enabled():
        obsolete = obsolete_filenames(previous, args.repo_dir, repo_name)
        if obsolete:
            purge_cloudflare_tags([f"pkg:{name}" for name in obsolete], args.dry_run)
        else:
            log("no obsolete package files to purge from the cache")

    if failed_any:
        log("publishing finished, but some packages failed to build (see the homepage)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
