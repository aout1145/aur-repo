#!/usr/bin/env python3
"""Build the packages from a plan produced by ``plan.py``.

For every pkgbase in the plan, in dependency order:

  * if it must be built, clone its AUR git repository, run ``makepkg`` (as a
    non-root user), optionally sign the result, install it into the running
    container so that later builds can use it, and add it to the local
    repository database if it should be published;
  * otherwise download the already published package from object storage and
    install it, so that dependents can be built against it.

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

from aur_lib import load_config, log, rclone_available, run  # noqa: E402

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
# Object storage
# ---------------------------------------------------------------------------
def rclone_get(remote_dir: str, filename: str, dest: str, dry_run: bool) -> bool:
    if not rclone_available():
        return False
    remote = remote_dir.rstrip("/") + "/" + filename
    proc = run(
        ["rclone", "copyto", remote, os.path.join(dest, filename)],
        check=False,
        capture_output=True,
        text=True,
        dry_run=dry_run,
    )
    return proc.returncode == 0


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
    parser.add_argument("--remote", default=os.environ.get("R2_PATH", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo = cfg["repo"]
    build_cfg = cfg["build"]
    repo_name = repo["name"]

    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    nodes = plan["nodes"]
    build_set = set(plan["build"])
    order = plan["order"]

    os.makedirs(args.repo_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)
    os.makedirs(args.downloads_dir, exist_ok=True)

    signing_key = os.environ.get("GPG_KEY", "")
    passphrase = os.environ.get("GPG_PASSPHRASE") or None
    if signing_key and os.environ.get("GPG_PRIVATE_KEY"):
        import_key(os.environ["GPG_PRIVATE_KEY"], args.dry_run)

    # Seed the local repository with the currently published database so that
    # packages which are not rebuilt keep their entries.
    repo_db = os.path.join(args.repo_dir, f"{repo_name}.db.tar.gz")
    if args.remote and rclone_available():
        remote_db = args.remote.rstrip("/") + f"/{repo_name}.db"
        proc = run(
            ["rclone", "copyto", remote_db, repo_db],
            check=False,
            capture_output=True,
            text=True,
            dry_run=args.dry_run,
        )
        if proc.returncode != 0:
            log("note: no published database found (first build)")

    # Download already published packages that we are not rebuilding but that
    # are needed as dependencies.
    if args.remote and rclone_available():
        for pkgbase, node in nodes.items():
            if pkgbase in build_set:
                continue
            for filename in node.get("install_files", []):
                if not os.path.exists(os.path.join(args.downloads_dir, filename)):
                    rclone_get(args.remote, filename, args.downloads_dir, args.dry_run)

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
                if not node["publish"]:
                    continue
                destination = os.path.join(args.repo_dir, os.path.basename(package))
                if not args.dry_run:
                    shutil.copy2(package, destination)
                if signing_key:
                    # Sign the copy that will be published so that the
                    # detached signature ends up next to it in the repo.
                    sign_file(destination, signing_key, passphrase, args.dry_run)
                publish_files.append(destination)

            # Install the freshly built package(s) so dependents can build.
            run_pacman_u(built, args.dry_run)
        else:
            files = [os.path.join(args.downloads_dir, f) for f in node.get("install_files", [])]
            missing = [f for f in files if not os.path.exists(f)]
            if missing and not args.dry_run:
                log(f"warning: missing published packages for dependency {pkgbase}: "
                    + ", ".join(os.path.basename(f) for f in missing))
            run_pacman_u([f for f in files if os.path.exists(f)], args.dry_run)

    # Update the repository database with the newly published packages.
    if publish_files:
        cmd = ["repo-add", "--nocolor", "-q"]
        if signing_key:
            cmd.append("--include-sigs")
        cmd.append(repo_db)
        cmd.extend(publish_files)
        run(cmd, dry_run=args.dry_run)

    # Pacman fetches `<name>.db`; keep a copy of the compressed db under that
    # name and detach-sign it if a key is configured.
    db_copy = os.path.join(args.repo_dir, f"{repo_name}.db")
    if os.path.exists(repo_db) and not args.dry_run:
        shutil.copy2(repo_db, db_copy)
        if signing_key:
            sign_file(db_copy, signing_key, passphrase, args.dry_run)

    # Publish the public key so clients can import it from the repository.
    if signing_key and not args.dry_run:
        public_key = os.path.join(args.repo_dir, "repo.gpg")
        with open(public_key, "wb") as fh:
            run(["gpg", "--export", "--armor", signing_key], stdout=fh)

    if not publish_files:
        log("no packages were built; nothing to publish")
    else:
        log(f"prepared {len(publish_files)} package(s) for publishing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
