#!/usr/bin/env python3
"""Compute the build plan.

Reads ``packages.toml`` and the currently published repository database,
resolves the full AUR dependency graph, and decides which pkgbases actually
need to be (re)built.  The result is written to ``plan.json``.

Nothing is rebuilt unless it is missing from the repository or its AUR version
is strictly newer than the published one (or the build was explicitly forced).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import (  # noqa: E402
    AurClient,
    Pacman,
    db_filenames_for,
    db_version_for,
    load_config,
    load_db,
    log,
    resolve,
    run,
)


def parse_filter(value: str) -> set[str]:
    if not value:
        return set()
    return {part for part in value.replace(",", " ").split() if part}


def fetch_db(remote_path: str, repo_name: str, dest: str) -> str | None:
    """Download ``<repo>.db`` from the object store.  Returns the local path."""
    if not remote_path or not os.environ.get("RCLONE_CONFIG_R2_ACCESS_KEY_ID"):
        return None
    remote = remote_path.rstrip("/") + f"/{repo_name}.db"
    proc = run(
        ["rclone", "copyto", remote, dest],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        log(f"note: no existing database at {remote} (first run?)")
        return None
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.toml")
    parser.add_argument("--db", help="path to the published .db file")
    parser.add_argument("--out", default="plan.json")
    parser.add_argument("--packages", default="", help="subset filter (names)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--update-vcs", action="store_true")
    parser.add_argument("--remote", default=os.environ.get("R2_PATH", ""))
    args = parser.parse_args()

    cfg = load_config(args.config)
    repo_name = cfg["repo"]["name"]
    build_opts = cfg["build"]

    db_path = args.db
    if not db_path and args.remote:
        db_path = fetch_db(args.remote, repo_name, "published.db")
    db = load_db(db_path) if db_path else {}

    client = AurClient()
    pacman = Pacman()

    # Map every configured package to its pkgbase and collect per-package
    # overrides keyed by pkgbase.
    options: dict[str, dict] = {}
    targets: list[str] = []
    wanted = parse_filter(args.packages)
    unmatched = set(wanted)
    for entry in cfg["packages"]:
        pkgbase = client.canonical_pkgbase(entry["name"])
        if not pkgbase:
            log(f"warning: skipping unknown AUR package '{entry['name']}'")
            continue
        if wanted and entry["name"] not in wanted and pkgbase not in wanted:
            continue
        unmatched.discard(entry["name"])
        unmatched.discard(pkgbase)
        if pkgbase not in options:
            targets.append(pkgbase)
        options[pkgbase] = {
            "skip_check": entry["skip_check"],
            "vcs": entry["vcs"],
            "update_vcs": entry["update_vcs"],
        }
    if wanted and unmatched:
        log(f"warning: filter entries not found in configuration: {', '.join(sorted(unmatched))}")
    if not targets:
        log("nothing to do: no packages selected")
        _write_outputs([], repo_name)
        _write_plan(args.out, repo_name, [], [], {}, cfg)
        return 0

    log(f"resolving AUR dependencies for: {', '.join(targets)}")
    graph = resolve(client, pacman, targets, run_checks=build_opts["run_checks"])
    nodes = graph["nodes"]
    order = graph["order"]
    target_set = {pb for pb in targets if pb in nodes}

    build: list[str] = []
    reasons: dict[str, str] = {}
    plan_nodes: dict[str, dict] = {}

    for pkgbase in order:
        node = nodes[pkgbase]
        overrides = options.get(pkgbase, {})
        skip_check = bool(overrides.get("skip_check")) or not build_opts["run_checks"]
        is_target = pkgbase in target_set
        present, version = db_version_for(db, node)
        deps_built = sorted(p for p in node["providers"] if p in set(build))
        update_vcs = overrides.get("update_vcs")
        if update_vcs is None:
            update_vcs = build_opts["update_vcs"]
        update_vcs = bool(update_vcs) or args.update_vcs
        vcs = overrides.get("vcs")
        if vcs is None:
            vcs = node["vcs"]

        reason: str | None = None
        if is_target:
            if args.force:
                reason = "forced"
            elif vcs and update_vcs:
                reason = "VCS update requested"
            elif not present:
                reason = "not yet published"
            elif version and pacman.vercmp(node["version"], version) > 0:
                reason = f"update {version} -> {node['version']}"
            elif build_opts["rebuild_dependents"] and deps_built:
                reason = "dependency rebuilt: " + ", ".join(deps_built)
        else:
            if vcs and update_vcs:
                reason = "VCS update requested"
            elif not present:
                reason = "AUR dependency not yet published"
            elif build_opts["rebuild_dependents"] and deps_built:
                reason = "dependency rebuilt: " + ", ".join(deps_built)
            elif not wanted and version and pacman.vercmp(node["version"], version) > 0:
                reason = f"AUR dependency update {version} -> {node['version']}"

        if reason:
            build.append(pkgbase)
            reasons[pkgbase] = reason

        plan_nodes[pkgbase] = {
            "version": node["version"],
            "pkgnames": node["pkgnames"],
            "vcs": bool(vcs),
            "publish": bool(node["publish"]),
            "target": is_target,
            "providers": node["providers"],
            "skip_check": skip_check,
            "present": present,
            "published_version": version,
            "install_files": [] if pkgbase in set(build) else db_filenames_for(db, node),
        }

    log("build plan:")
    if not build:
        log("  (everything is up to date)")
    for pkgbase in order:
        marker = "BUILD" if pkgbase in set(build) else " ok  "
        node = plan_nodes[pkgbase]
        detail = reasons.get(pkgbase, "up to date")
        pub = "publish" if node["publish"] else "build-only"
        log(f"  [{marker}] {pkgbase:28} {node['version']:24} {pub:10} {detail}")

    _write_plan(args.out, repo_name, order, build, plan_nodes, cfg)
    _write_outputs(build, repo_name)
    return 0


def _write_plan(path: str, repo_name: str, order: list[str], build: list[str],
                nodes: dict, cfg: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "repo": cfg["repo"],
                "order": order,
                "build": build,
                "nodes": nodes,
            },
            fh,
            indent=2,
        )
        fh.write("\n")
    log(f"wrote {path}")


def _write_outputs(build: list[str], repo_name: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as fh:
        fh.write(f"has_build={'true' if build else 'false'}\n")
        fh.write(f"build_list={' '.join(build)}\n")
        fh.write(f"repo_name={repo_name}\n")


if __name__ == "__main__":
    raise SystemExit(main())
