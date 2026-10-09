#!/usr/bin/env python3
"""Build the packages from a plan produced by ``plan.py``.

For every pkgbase in the plan, in dependency order:

  * if it must be built, clone its AUR git repository, run ``makepkg`` (as a
    non-root user), sign the result if configured, install it into the running
    container so later builds can use it, and add it to the local repository
    database;
  * otherwise download the already published package from the GitHub release
    and install it, so dependents can be built against it.

Only packages that something else in the graph depends on are installed into
the build container.  This keeps mutually conflicting packages (for example
``cpeditor`` and ``cpeditor-bin``) from clashing at build time, even though
they are allowed to coexist in the published repository.

Package files are renamed to a GitHub-release-safe name *before* they are added
to the repository database, so the database's ``%FILENAME%`` matches the asset
name (GitHub renames characters such as ``:`` and ``+`` otherwise).

The assembled repository is left in ``--repo-dir`` for ``publish.py``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
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


def run_makepkg(workdir: str, flags: list[str], builder_user: str | None, dry_run: bool) -> None:
    makepkg = _which("makepkg")
    if is_root() and builder_user:
        inner = f"cd {shlex.quote(workdir)} && {makepkg} {' '.join(flags)}"
        cmd = ["sudo", "-u", builder_user, "-H", "bash", "-lc", inner]
    else:
        cmd = ["bash", "-lc", f"cd {shlex.quote(workdir)} && {makepkg} {' '.join(flags)}"]
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

    publish_files: list[str] = []

    for pkgbase in order:
        node = nodes[pkgbase]
        if pkgbase in build_set:
            workdir = os.path.join(args.work_dir, pkgbase)
            if os.path.exists(workdir):
                shutil.rmtree(workdir)
            log(f"=== building {pkgbase} ({node['version']}) ===")
            run(
                ["git", "clone", "--depth", "1", f"{AUR_GIT}/{pkgbase}.git", workdir],
                dry_run=args.dry_run,
            )
            if is_root() and args.builder_user and not args.dry_run:
                run(["chown", "-R", f"{args.builder_user}:{args.builder_user}", workdir])
            run_makepkg(workdir, makepkg_flags(node, build_cfg), args.builder_user, args.dry_run)

            built = sorted(
                p for p in glob.glob(os.path.join(workdir, "*.pkg.tar.*"))
                if not p.endswith(".sig")
            )
            if not built and not args.dry_run:
                raise SystemExit(f"build of {pkgbase} produced no packages")

            for package in built:
                # Rename to a GitHub-release-safe asset name.  repo-add records
                # this name as %FILENAME%, so clients download the same file.
                safe = sanitize_filename(os.path.basename(package))
                destination = os.path.join(args.repo_dir, safe)
                if not args.dry_run:
                    shutil.copy2(package, destination)
                if signing_key:
                    sign_file(destination, signing_key, passphrase, args.dry_run)
                publish_files.append(destination)

            # Install the freshly built package(s) only if something in this
            # graph depends on them, so dependents can build.
            if pkgbase in needed:
                run_pacman_u(built, args.dry_run)
            else:
                log(f"not installing {pkgbase}: no package in this run depends on it")
        else:
            if pkgbase not in needed:
                continue
            files = [os.path.join(args.downloads_dir, f) for f in node.get("install_files", [])]
            missing = [f for f in files if not os.path.exists(f)]
            if missing and not args.dry_run:
                log(f"warning: missing published packages for dependency {pkgbase}: "
                    + ", ".join(os.path.basename(f) for f in missing))
            run_pacman_u([f for f in files if os.path.exists(f)], args.dry_run)

    # Update the repository database with the newly built packages.
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

    if publish_files:
        log(f"prepared {len(publish_files)} package(s) for publishing")
    elif remove:
        log("no packages built; updated the database for removals")
    else:
        log("no packages were built; nothing to publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
