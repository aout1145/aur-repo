#!/usr/bin/env python3
"""Record the outcome of the workflow run.

Writes ``status.json`` and uploads it to the same release as the repository
(the tag from ``[repo].tag``).  This job runs with ``if: always()`` so that a
failed build is visible on the repository homepage, while the packages and the
database themselves are left untouched on failure.

The result of the other jobs is passed in through the environment:
``PLAN_RESULT``, ``BUILD_RESULT`` and ``PUBLISH_RESULT`` (the ``needs.*.result``
values).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aur_lib import (  # noqa: E402
    gh_available,
    gh_ensure_release,
    github_repo,
    load_config,
    log,
    run,
)

RESULTS = ("PLAN_RESULT", "BUILD_RESULT", "PUBLISH_RESULT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.toml")
    parser.add_argument("--repo", default=github_repo(), help="owner/repo (defaults to $GITHUB_REPOSITORY)")
    parser.add_argument("--out", default="status.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tag = cfg["repo"].get("tag", "repo")

    results = {name: os.environ.get(name, "") for name in RESULTS}
    if any(value == "failure" for value in results.values()):
        conclusion = "failure"
    elif any(value == "cancelled" for value in results.values()):
        conclusion = "cancelled"
    else:
        conclusion = "success"

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    status = {
        "conclusion": conclusion,
        # BUILD_RESULT is "success" only when packages were actually built.
        "changed": results.get("BUILD_RESULT") == "success",
        "last_run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_url": f"{server}/{args.repo}/actions/runs/{run_id}" if run_id else "",
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    if not args.repo or not gh_available():
        if args.dry_run:
            log("dry run: repository or gh CLI not available; not uploading")
            return 0
        raise SystemExit("status: no repository configured or the 'gh' CLI is missing")

    gh_ensure_release(args.repo, tag, args.dry_run)
    run(
        ["gh", "release", "upload", tag, args.out, "--repo", args.repo, "--clobber"],
        dry_run=args.dry_run,
    )
    log(f"recorded run status: {conclusion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
