"""Shared helpers for the AUR build system.

This module knows how to:
  * read the project configuration (``packages.toml``)
  * talk to the AUR RPC / cgit API
  * parse ``.SRCINFO`` files
  * resolve the AUR dependency graph (including AUR-only dependencies)
  * read a pacman repository database and compare versions
  * run small external helpers (``pacman``, ``vercmp``, ``gh``, ``gpg``)

Only the Python standard library plus PyYAML are required.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque

AUR_RPC = "https://aur.archlinux.org/rpc/v5"
AUR_CGIT = "https://aur.archlinux.org/cgit/aur.git/plain"
AUR_GIT = "https://aur.archlinux.org"

# Source URLs beginning with one of these schemes mark a "VCS" package whose
# pkgver is computed at build time and therefore cannot be compared against the
# version published in the repository.
VCS_SCHEMES = ("git+", "svn+", "hg+", "bzr+", "darcs+", "cvs://")


# Some environments advertise an IPv6 address but have no working IPv6 route.
# urllib then blocks until the socket times out before retrying over IPv4,
# which made every API call take ~30s.  Prefer IPv4 unless explicitly disabled.
if os.environ.get("AUR_IPV4", "1").lower() not in ("0", "false", "no"):
    _original_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A001
        return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _getaddrinfo


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url: str, retries: int = 4, timeout: int = 30) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aur-ci/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 - network errors are retried
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "repo": {
        "name": "custom",
        "arch": "x86_64",
        "tag": "repo",
        "remove_old": False,
    },
    "build": {
        "run_checks": False,
        "rebuild_dependents": False,
        "update_vcs": False,
        "skip_pgp_check": False,
    },
    "packages": [],
}


def load_config(path: str) -> dict:
    import tomllib

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = {
        "repo": {**DEFAULT_CONFIG["repo"], **(raw.get("repo") or {})},
        "build": {**DEFAULT_CONFIG["build"], **(raw.get("build") or {})},
        "packages": [],
    }

    overrides = raw.get("overrides") or {}
    for name in raw.get("packages") or []:
        if not isinstance(name, str):
            raise ValueError(f"invalid package entry: {name!r}")
        ov = overrides.get(name) or {}
        cfg["packages"].append(
            {
                "name": name,
                "skip_check": bool(ov.get("skip_check", False)),
                "vcs": ov.get("vcs"),
                "update_vcs": ov.get("update_vcs"),
            }
        )
    return cfg


# ---------------------------------------------------------------------------
# AUR client
# ---------------------------------------------------------------------------
class NotFound(Exception):
    """Raised when a package/base does not exist."""


class AurClient:
    def __init__(self) -> None:
        self._info_cache: dict[str, list[dict]] = {}
        self._src_cache: dict[str, str] = {}
        self._pkgbase_cache: dict[str, str | None] = {}

    def info(self, names: list[str]) -> list[dict]:
        results: list[dict] = []
        names = list(dict.fromkeys(n for n in names if n))
        for i in range(0, len(names), 100):
            chunk = names[i : i + 100]
            query = "&".join("arg[]=" + urllib.parse.quote(n) for n in chunk)
            data = json.loads(http_get(f"{AUR_RPC}/info?{query}"))
            results.extend(data.get("results", []))
        return results

    def search(self, term: str, by: str | None = None) -> list[dict]:
        url = f"{AUR_RPC}/search/{urllib.parse.quote(term)}"
        if by:
            url += f"?by={urllib.parse.quote(by)}"
        return json.loads(http_get(url)).get("results", [])

    def srcinfo(self, pkgbase: str) -> str:
        if pkgbase in self._src_cache:
            return self._src_cache[pkgbase]
        cache_dir = os.environ.get("AUR_CACHE")
        cache_file = os.path.join(cache_dir, f"{pkgbase}.srcinfo") if cache_dir else None
        if cache_file and os.path.isfile(cache_file):
            with open(cache_file, "r", encoding="utf-8") as fh:
                text = fh.read()
            if "pkgbase" in text:
                self._src_cache[pkgbase] = text
                return text
        try:
            text = http_get(
                f"{AUR_CGIT}/.SRCINFO?h={urllib.parse.quote(pkgbase)}"
            ).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise NotFound(pkgbase) from exc
        except RuntimeError as exc:
            raise NotFound(pkgbase) from exc
        if "pkgbase" not in text:
            raise NotFound(pkgbase)
        self._src_cache[pkgbase] = text
        if cache_file:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
        return text

    def canonical_pkgbase(self, name: str) -> str | None:
        """Map a package name or pkgbase to its pkgbase."""
        if name in self._pkgbase_cache:
            return self._pkgbase_cache[name]
        result: str | None = None
        # A pkgbase normally also exists as a package name, so try .SRCINFO
        # first (cheap and authoritative for the base itself).
        try:
            self.srcinfo(name)
            result = name
        except NotFound:
            for entry in self.info([name]):
                if entry["Name"] == name or entry["PackageBase"] == name:
                    result = entry["PackageBase"]
                    break
            if result is None:
                for entry in self.search(name, by="provides"):
                    if entry["Name"] == name:
                        result = entry["PackageBase"]
                        break
                else:
                    hits = self.search(name, by="provides")
                    if hits:
                        result = hits[0]["PackageBase"]
        self._pkgbase_cache[name] = result
        return result

    def pkgbase_for_dependencies(self, names: list[str]) -> dict[str, str | None]:
        """Batch-resolve dependency names to pkgbases.

        Package names are resolved with a single (batched) ``info`` request.
        Only names that are not package names (virtual ``provides``) fall back
        to a per-name ``provides`` search, which is rare in practice.
        """
        result: dict[str, str | None] = {}
        pending = [n for n in dict.fromkeys(names) if n]
        if not pending:
            return result
        for entry in self.info(pending):
            if entry["Name"] in pending:
                result[entry["Name"]] = entry["PackageBase"]
        for name in pending:
            if name in result:
                continue
            hits = self.search(name, by="provides")
            if hits:
                result[name] = hits[0]["PackageBase"]
                continue
            try:
                self.srcinfo(name)
                result[name] = name
            except NotFound:
                result[name] = None
        return result

    def pkgbase_for_dependency(self, name: str) -> str | None:
        return self.pkgbase_for_dependencies([name]).get(name)


# ---------------------------------------------------------------------------
# .SRCINFO parsing
# ---------------------------------------------------------------------------
def parse_srcinfo(text: str) -> dict:
    base: dict[str, list[str]] = {}
    packages: dict[str, dict[str, list[str]]] = {}
    current = base
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "pkgbase":
            base["pkgbase"] = [value]
            current = base
            continue
        if key == "pkgname":
            current = packages.setdefault(value, {})
            continue
        current.setdefault(key, []).append(value)

    def get(mapping: dict, key: str) -> list[str]:
        return mapping.get(key, [])

    pkgnames = list(packages.keys())
    depends = get(base, "depends") + [
        d for pkg in packages.values() for d in get(pkg, "depends")
    ]
    provides = get(base, "provides") + [
        p for pkg in packages.values() for p in get(pkg, "provides")
    ]
    sources = get(base, "source")
    epoch = (get(base, "epoch") or [None])[0]
    pkgver = (get(base, "pkgver") or [""])[0]
    pkgrel = (get(base, "pkgrel") or [""])[0]

    return {
        "pkgbase": (get(base, "pkgbase") or [""])[0],
        "pkgnames": pkgnames or [(get(base, "pkgbase") or [""])[0]],
        "pkgver": pkgver,
        "pkgrel": pkgrel,
        "epoch": epoch,
        "version": f"{epoch}:{pkgver}-{pkgrel}" if epoch else f"{pkgver}-{pkgrel}",
        "depends": depends,
        "makedepends": get(base, "makedepends"),
        "checkdepends": get(base, "checkdepends"),
        "provides": provides,
        "vcs": any(s.startswith(VCS_SCHEMES) for s in sources),
    }


def dep_name(spec: str) -> str:
    """Strip a version constraint from a dependency specification."""
    return re.split(r"[<>=]", spec, 1)[0].strip()


# GitHub release assets may only contain alphanumerics and ``. - _``; anything
# else (notably the ``:`` used for Arch package epochs, and ``+``) is renamed
# by GitHub, which would make the filename stored in the repository database
# wrong.  Sanitise locally instead, before ``repo-add`` sees the file.
_GH_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename(name: str) -> str:
    return _GH_UNSAFE.sub("_", name)


def github_repo() -> str:
    """``owner/repo`` from the environment (set by GitHub Actions)."""
    return os.environ.get("GITHUB_REPOSITORY", "")


# ---------------------------------------------------------------------------
# pacman / vercmp
# ---------------------------------------------------------------------------
class Pacman:
    def __init__(self, dbpath: str = "/var/lib/pacman") -> None:
        self._dbpath = dbpath
        self._official_set: set[str] | None = None
        self._official_cache: dict[str, bool] = {}

    def _load_official(self) -> None:
        """Collect every package name and ``provides`` entry from the sync
        databases.  This is a single pass over ``$dbpath/sync/*.db`` and is far
        faster than spawning ``pacman`` once per dependency."""
        names: set[str] = set()
        for path in glob.glob(os.path.join(self._dbpath, "sync", "*.db")):
            try:
                with tarfile.open(path, "r:*") as archive:
                    for member in archive.getmembers():
                        if not member.name.endswith("/desc"):
                            continue
                        handle = archive.extractfile(member)
                        if handle is None:
                            continue
                        desc = _parse_desc(handle.read().decode("utf-8", "replace"))
                        if desc.get("NAME"):
                            names.add(desc["NAME"])
                        provides = desc.get("PROVIDES")
                        if isinstance(provides, str):
                            provides = [provides]
                        for provide in provides or []:
                            names.add(dep_name(provide))
            except Exception as exc:  # noqa: BLE001 - fall back to pacman below
                log(f"warning: could not read sync database {path}: {exc}")
        self._official_set = names

    def _sp_check(self, name: str) -> bool:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        proc = subprocess.run(
            ["pacman", "-Sp", "--print-format", "%n", "--noconfirm", name],
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode == 0:
            return True
        return "target not found" not in proc.stderr

    def is_official(self, name: str) -> bool:
        """Whether ``name`` (a package name *or* a virtual provide) exists in
        the configured official repositories."""
        if name in self._official_cache:
            return self._official_cache[name]
        if self._official_set is None:
            self._load_official()
        if self._official_set:
            # A missing name may still be provided by an installed local
            # package, but for building from scratch only sync repos matter.
            ok = name in self._official_set
        else:
            ok = self._sp_check(name)
        self._official_cache[name] = ok
        return ok

    @staticmethod
    def vercmp(a: str, b: str) -> int:
        proc = subprocess.run(["vercmp", a, b], capture_output=True, text=True)
        return int(proc.stdout.strip() or "0")


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------
def resolve(client: AurClient, pacman: Pacman, targets: list[str], run_checks: bool) -> dict:
    """Resolve the full AUR build graph rooted at ``targets`` (pkgbases)."""
    nodes: dict[str, dict] = {}
    name_index: dict[str, str] = {}  # pkgname/provides -> pkgbase
    queue: deque[str] = deque(targets)
    seen: set[str] = set()

    while queue:
        pkgbase = queue.popleft()
        if pkgbase in seen:
            continue
        seen.add(pkgbase)
        try:
            node = parse_srcinfo(client.srcinfo(pkgbase))
        except NotFound:
            log(f"warning: .SRCINFO for '{pkgbase}' not found, skipping")
            continue
        nodes[pkgbase] = node
        name_index.setdefault(pkgbase, pkgbase)
        for name in node["pkgnames"]:
            name_index[name] = pkgbase
        for provide in node["provides"]:
            name_index.setdefault(dep_name(provide), pkgbase)

        deps = list(node["depends"]) + list(node["makedepends"])
        if run_checks:
            deps += list(node["checkdepends"])
        unresolved: list[str] = []
        for dep in deps:
            name = dep_name(dep)
            if not name or name in name_index:
                continue
            if pacman.is_official(name):
                continue
            unresolved.append(name)
        unresolved = list(dict.fromkeys(unresolved))
        if not unresolved:
            continue
        for name, provider in client.pkgbase_for_dependencies(unresolved).items():
            if provider and provider not in seen:
                queue.append(provider)
            elif not provider:
                log(f"warning: cannot resolve dependency '{name}' (needed by {pkgbase})")

    # Build edges used for ordering (a provider must be built before the
    # package that depends on it).
    build_edges: set[tuple[str, str]] = set()
    providers: dict[str, set[str]] = defaultdict(set)
    for pkgbase, node in nodes.items():
        all_deps = {dep_name(d) for d in node["depends"]}
        all_deps |= {dep_name(d) for d in node["makedepends"]}
        if run_checks:
            all_deps |= {dep_name(d) for d in node["checkdepends"]}
        for name in all_deps:
            if not name or pacman.is_official(name):
                continue
            provider = name_index.get(name)
            if provider is None or provider == pkgbase:
                continue
            build_edges.add((provider, pkgbase))
            providers[pkgbase].add(provider)

    order = _topo_sort(nodes, build_edges)

    return {
        "order": order,
        "nodes": {
            pkgbase: {
                **node,
                # Every package in the dependency graph is published.  This
                # keeps the repository self-contained and, crucially, lets the
                # next run see that a dependency is already up to date instead
                # of rebuilding it from scratch.
                "publish": True,
                "target": pkgbase in set(targets),
                "providers": sorted(providers.get(pkgbase, set())),
            }
            for pkgbase, node in nodes.items()
        },
    }


def _topo_sort(nodes: dict[str, dict], edges: set[tuple[str, str]]) -> list[str]:
    indegree = {name: 0 for name in nodes}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        if target not in adjacency[source]:
            adjacency[source].add(target)
            indegree[target] += 1

    queue = deque(sorted(n for n, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in sorted(adjacency.get(node, ())):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) < len(nodes):
        leftover = sorted(n for n in nodes if n not in order)
        log(f"warning: dependency cycle detected: {', '.join(leftover)}")
        order.extend(leftover)
    return order


# ---------------------------------------------------------------------------
# Repository database
# ---------------------------------------------------------------------------
def load_db(path: str) -> dict[str, dict]:
    """Read a pacman database into ``{pkgname: desc}``."""
    if not path or not os.path.exists(path):
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            ["bsdtar", "-xf", path, "-C", tmp], capture_output=True, text=True
        )
        if proc.returncode != 0:
            log(f"warning: could not read repository database {path}: {proc.stderr.strip()}")
            return {}
        result: dict[str, dict] = {}
        for entry in sorted(os.listdir(tmp)):
            desc = os.path.join(tmp, entry, "desc")
            if not os.path.isfile(desc):
                continue
            with open(desc, "r", encoding="utf-8", errors="replace") as fh:
                parsed = _parse_desc(fh.read())
            if parsed.get("NAME"):
                result[parsed["NAME"]] = parsed
        return result


def _parse_desc(text: str) -> dict:
    result: dict[str, list[str]] = {}
    key: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            key = None
        elif line.startswith("%") and line.endswith("%"):
            key = line.strip("%")
            result.setdefault(key, [])
        elif key is not None:
            result[key].append(line)
    return {k: (v[0] if len(v) == 1 else v) for k, v in result.items()}


def db_version_for(db: dict[str, dict], node: dict) -> tuple[bool, str | None]:
    """Return ``(present, version)`` for a resolved node in the database."""
    for pkgname in node["pkgnames"]:
        if pkgname not in db:
            return False, None
    versions = {db[n]["VERSION"] for n in node["pkgnames"] if n in db}
    if not versions:
        return False, None
    return True, sorted(versions)[0]


def db_filenames_for(db: dict[str, dict], node: dict) -> list[str]:
    return [db[n]["FILENAME"] for n in node["pkgnames"] if n in db and "FILENAME" in db[n]]


# ---------------------------------------------------------------------------
# External helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], dry_run: bool = False, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    log("+ " + " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, check=check, **kwargs)


def gh_available() -> bool:
    return shutil.which("gh") is not None
