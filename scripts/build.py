#!/usr/bin/env python3
"""Build the packages from a plan produced by ``plan.py``.

Packages are built in dependency order.  A package that fails to build does not
abort the whole run: the failed package and its whole dependency *component*
are "locked" (their published files and database entries are left untouched)
while everything else is still built and published.

Locking works on the undirected dependency graph: if any node fails, its entire
connected component is locked.  This locks the failed package together with its
dependencies, and also every package that shares a dependency with it.

A summary (``result.json``) is written into ``--repo-dir`` for ``publish.py``:

    failed / skipped / locked / published          (pkgbases)
    failed_names / blocked_names / published_names (pkgnames)
    new_entries                                    (failed or locked packages
                                                    that were never published)
    failed_any

The assembled repository is left in ``--repo-dir`` for ``publish.py``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import (  # noqa: E402
    gh_available,
    github_repo,
    load_config,
    log,
    run,
    sanitize_filename,
)

AUR_GIT = "https://aur.archlinux.org"

# makepkg config used for every build: it drops the `debug` option so that no
# -debug packages are produced.  Anything that still slips through (a PKGBUILD
# that explicitly enables debug) is filtered out below as well.
MAKEPKG_CONF = "/tmp/makepkg-ci.conf"
_DEBUG_FILE_RE = re.compile(r"-debug-.*\.pkg\.tar\.[A-Za-z0-9]+$")


# ---------------------------------------------------------------------------
# Privilege helpers
# ---------------------------------------------------------------------------
def is_root() -> bool:
    return os.geteuid() == 0


def makepkg_flags(node: dict, build_cfg: dict) -> list[str]:
    flags = ["-s", "-f", "--noconfirm"]
    if node.get("skip_check") or not build_cfg.get("run_checks"):
        flags.append("--nocheck")
    if build_cfg.get("skip_pgp_check"):
        flags.append("--skippgpcheck")
    return flags


def write_makepkg_conf(dry_run: bool) -> None:
    """Write a makepkg config with the `debug` option removed."""
    content = (
        "source /etc/makepkg.conf\n"
        "OPTIONS=($(printf '%s\\n' \"${OPTIONS[@]}\" | grep -vx debug))\n"
    )
    log(f"# writing {MAKEPKG_CONF} without the debug option")
    if dry_run:
        return
    with open(MAKEPKG_CONF, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(MAKEPKG_CONF, 0o644)


def run_makepkg(workdir: str, flags: list[str], builder_user: str | None, dry_run: bool) -> None:
    makepkg = _which("makepkg")
    command = f"{makepkg} --config {shlex.quote(MAKEPKG_CONF)} {' '.join(flags)}"
    if is_root() and builder_user:
        cmd = ["sudo", "-u", builder_user, "-H", "bash", "-lc", f"cd {shlex.quote(workdir)} && {command}"]
    else:
        cmd = ["bash", "-lc", f"cd {shlex.quote(workdir)} && {command}"]
    run(cmd, dry_run=dry_run)


def run_pacman_u(files: list[str], dry_run: bool) -> None:
    if not files:
        return
    pacman = ["pacman"] if is_root() else ["sudo", "pacman"]
    run([*pacman, "-U", "--noconfirm", "--needed", *files], dry_run=dry_run)


def _which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required tool not found: {name}")
    return path


# ---------------------------------------------------------------------------
# GitHub Releases
# ---------------------------------------------------------------------------
def gh_release_download(repo: str, tag: str, filename: str, dest_dir: str, dry_run: bool) -> bool:
    if not repo or not gh_available():
        return False
    proc = run(
        [
            "gh", "release", "download", tag,
            "--repo", repo,
            "--pattern", filename,
            "--dir", dest_dir,
            "--clobber",
        ],
        check=False,
        capture_output=True,
        text=True,
        dry_run=dry_run,
    )
    return proc.returncode == 0 and os.path.exists(os.path.join(dest_dir, filename))


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------
def sign_file(path: str, key: str, passphrase: str | None, dry_run: bool) -> None:
    args = ["gpg", "--batch", "--yes", "--no-tty", "--detach-sign", "--no-armor", "-u", key]
    stdin_data: str | None = None
    if passphrase:
        # Never put the passphrase on the command line (it would show up in the
        # log and in the process list); read it from stdin instead.
        args += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
        stdin_data = passphrase
    args.append(path)
    log("+ gpg --detach-sign --no-armor -u <key> " + shlex.quote(path))
    if dry_run:
        return
    subprocess.run(args, check=True, input=stdin_data, text=True)


def import_key(data: str, dry_run: bool) -> None:
    run(["gpg", "--batch", "--import"], input=data, text=True, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def locked_components(nodes: dict, failed: set[str]) -> set[str]:
    """Return every node connected (in either direction) to a failed node."""
    adjacency: dict[str, set[str]] = {name: set() for name in nodes}
    for pkgbase, node in nodes.items():
        for provider in node.get("providers", []):
            adjacency.setdefault(provider, set()).add(pkgbase)
            adjacency.setdefault(pkgbase, set()).add(provider)

    locked: set[str] = set()
    stack = [name for name in failed if name in nodes]
    while stack:
        current = stack.pop()
        if current in locked:
            continue
        locked.add(current)
        stack.extend(adjacency.get(current, ()))
    return locked


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.toml")
    parser.add_argument("--plan", default="plan.json")
    parser.add_argument("--repo-dir", default="public")
    parser.add_argument("--work-dir", default="work")
    parser.add_argument("--downloads-dir", default="downloads")
    parser.add_argument("--builder-user", default=os.environ.get("BUILDER_USER", "builder"))
    parser.add_argument("--repo", default=github_repo(), help="owner/repo (defaults to $GITHUB_REPOSITORY)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo_name = cfg["repo"]["name"]
    tag = cfg["repo"].get("tag", "repo")
    build_cfg = cfg["build"]

    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    nodes = plan["nodes"]
    build_set = set(plan["build"])
    order = plan["order"]
    remove = list(plan.get("remove", []))

    # Only packages that something else in this graph depends on need to be
    # installed into the build container.  Installing every package would fail
    # for mutually conflicting packages (e.g. cpeditor and cpeditor-bin) that
    # are perfectly fine to coexist in the published repository.
    needed: set[str] = set()
    for node in nodes.values():
        needed.update(node.get("providers", []))

    os.makedirs(args.repo_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)
    os.makedirs(args.downloads_dir, exist_ok=True)

    # Build without the `debug` option (no -debug packages).
    write_makepkg_conf(args.dry_run)

    signing_key = os.environ.get("GPG_KEY", "")
    passphrase = os.environ.get("GPG_PASSPHRASE") or None
    if signing_key and os.environ.get("GPG_PRIVATE_KEY"):
        import_key(os.environ["GPG_PRIVATE_KEY"], args.dry_run)

    # Seed the local repository with the currently published database so that
    # packages which are not rebuilt keep their entries.  GitHub serves the
    # asset as `<name>.db`; repo-add wants the compressed-suffix form.
    repo_db = os.path.join(args.repo_dir, f"{repo_name}.db.tar.gz")
    downloaded_db = os.path.join(args.repo_dir, f"{repo_name}.db")
    if args.repo and gh_available():
        if gh_release_download(args.repo, tag, f"{repo_name}.db", args.repo_dir, args.dry_run):
            if not args.dry_run:
                os.replace(downloaded_db, repo_db)
        else:
            log("note: no published database found (first build)")

    # Drop packages that are no longer part of the resolved graph (removed from
    # packages.toml or no longer needed as a dependency).
    if remove and os.path.exists(repo_db):
        log("removing from the repository database: " + ", ".join(remove))
        run(["repo-remove", "--nocolor", "-q", repo_db, *remove], dry_run=args.dry_run)

    # Download already published packages that we are not rebuilding but that
    # are needed as dependencies.
    if args.repo and gh_available():
        for pkgbase, node in nodes.items():
            if pkgbase in build_set or pkgbase not in needed:
                continue
            for filename in node.get("install_files", []):
                if not os.path.exists(os.path.join(args.downloads_dir, filename)):
                    gh_release_download(args.repo, tag, filename, args.downloads_dir, args.dry_run)

    failed: set[str] = set()      # own build failed
    skipped: set[str] = set()     # not attempted because a dependency failed
    built_files: dict[str, list[str]] = {}

    for pkgbase in order:
        node = nodes[pkgbase]
        if pkgbase in build_set:
            if any(p in failed or p in skipped for p in node.get("providers", [])):
                log(f"=== skipping {pkgbase}: a dependency failed to build ===")
                skipped.add(pkgbase)
                continue

            workdir = os.path.join(args.work_dir, pkgbase)
            if os.path.exists(workdir):
                shutil.rmtree(workdir)
            log(f"=== building {pkgbase} ({node['version']}) ===")
            try:
                run(
                    ["git", "clone", "--depth", "1", f"{AUR_GIT}/{pkgbase}.git", workdir],
                    dry_run=args.dry_run,
                )
                if is_root() and args.builder_user and not args.dry_run:
                    run(["chown", "-R", f"{args.builder_user}:{args.builder_user}", workdir])
                run_makepkg(workdir, makepkg_flags(node, build_cfg), args.builder_user, args.dry_run)

                built = [
                    p for p in glob.glob(os.path.join(workdir, "*.pkg.tar.*"))
                    if not p.endswith(".sig") and not _DEBUG_FILE_RE.search(os.path.basename(p))
                ]
                for other in glob.glob(os.path.join(workdir, "*.pkg.tar.*")):
                    if _DEBUG_FILE_RE.search(os.path.basename(other)):
                        log(f"skipping debug package {os.path.basename(other)}")
                if not built and not args.dry_run:
                    raise RuntimeError("build produced no packages")
                built_files[pkgbase] = built

                # Install the freshly built package(s) only if something in
                # this graph depends on them, so dependents can build.
                if pkgbase in needed:
                    run_pacman_u(built, args.dry_run)
                else:
                    log(f"not installing {pkgbase}: no package in this run depends on it")
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                log(f"!!! build failed: {pkgbase}: {exc}")
                failed.add(pkgbase)
        else:
            if pkgbase not in needed:
                continue
            files = [os.path.join(args.downloads_dir, f) for f in node.get("install_files", [])]
            missing = [f for f in files if not os.path.exists(f)]
            if missing and not args.dry_run:
                log(f"warning: missing published packages for dependency {pkgbase}: "
                    + ", ".join(os.path.basename(f) for f in missing))
            try:
                run_pacman_u([f for f in files if os.path.exists(f)], args.dry_run)
            except subprocess.CalledProcessError as exc:
                log(f"warning: could not install dependency {pkgbase}: {exc}")

    # A failed package locks its whole dependency component: itself, its
    # dependencies, and every package that shares a dependency with it.
    locked = locked_components(nodes, failed)
    blocked = (build_set & locked) - failed
    published = [pb for pb in order if pb in build_set and pb not in locked and pb in built_files]

    # Copy the packages that may be published (successful and not locked).
    publish_files: list[str] = []
    for pkgbase in published:
        for package in built_files[pkgbase]:
            safe = sanitize_filename(os.path.basename(package))
            destination = os.path.join(args.repo_dir, safe)
            if not args.dry_run:
                shutil.copy2(package, destination)
            if signing_key:
                sign_file(destination, signing_key, passphrase, args.dry_run)
            publish_files.append(destination)

    # Update the repository database with the newly published packages.  Locked
    # packages keep their previous entries.
    if publish_files:
        cmd = ["repo-add", "--nocolor", "-q"]
        if signing_key:
            cmd.append("--include-sigs")
        cmd.append(repo_db)
        cmd.extend(publish_files)
        run(cmd, dry_run=args.dry_run)

    # Pacman fetches `<name>.db`; repo-add/repo-remove create it as a symlink to
    # `<name>.db.tar.gz`.  Replace it with a real file so it can be signed and
    # uploaded as-is (and so copying never hits "same file").
    db_copy = os.path.join(args.repo_dir, f"{repo_name}.db")
    if os.path.exists(repo_db) and not args.dry_run:
        if os.path.islink(db_copy) or os.path.exists(db_copy):
            os.remove(db_copy)
        shutil.copy2(repo_db, db_copy)
        if signing_key:
            sign_file(db_copy, signing_key, passphrase, args.dry_run)

    # Publish the public key so clients can import it from the repository.
    if signing_key and not args.dry_run:
        public_key = os.path.join(args.repo_dir, "repo.gpg")
        with open(public_key, "wb") as fh:
            run(["gpg", "--export", "--armor", signing_key], stdout=fh)

    _write_result(args.repo_dir, nodes, failed, skipped, locked, blocked, published)

    if failed or skipped:
        log(f"build finished with failures: {len(failed)} failed, {len(skipped)} skipped, "
            f"{len(published)} published")
    else:
        log(f"build finished: {len(published)} package(s) published")
    return 0


def _names(nodes: dict, bases: set[str]) -> list[str]:
    names: set[str] = set()
    for pkgbase in bases:
        node = nodes.get(pkgbase)
        if node:
            names.update(node.get("pkgnames", [pkgbase]))
        else:
            names.add(pkgbase)
    return sorted(names)


def _write_result(repo_dir: str, nodes: dict, failed: set[str], skipped: set[str],
                  locked: set[str], blocked: set[str], published: list[str]) -> None:
    # Packages that were never published have no file/version to show, so they
    # are listed separately for the homepage (the "new package failed" case).
    new_entries = []
    for pkgbase in sorted(set(failed) | set(blocked)):
        node = nodes.get(pkgbase)
        if node and not node.get("present", False):
            new_entries.append({
                "name": pkgbase,
                "version": node.get("version", ""),
                "status": "failed" if pkgbase in failed else "blocked",
            })

    result = {
        "failed": sorted(failed),
        "skipped": sorted(skipped),
        "locked": sorted(locked),
        "published": sorted(published),
        "failed_names": _names(nodes, failed),
        "blocked_names": _names(nodes, blocked),
        "published_names": _names(nodes, set(published)),
        "new_entries": new_entries,
        "failed_any": bool(failed or skipped),
    }
    path = os.path.join(repo_dir, "result.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
