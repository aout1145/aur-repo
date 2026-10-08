// Cloudflare Worker that fronts the pacman repository stored as GitHub release
// assets.
//
// SECURITY: this is intentionally NOT an open proxy.  Only these paths are
// served:
//   * the repository database           <DB_NAME> and <DB_NAME>.sig
//   * package files and signatures      *.pkg.tar.<ext> [.sig]
//   * the repository public key         repo.gpg
// Everything else returns 404, so the domain cannot be used to proxy arbitrary
// content (which could get it flagged for phishing).
//
// Package files are immutable and cached at the edge for a year; the database
// and the public key are always fetched fresh so a database is never served
// with a mismatched signature.
//
// Configure GITHUB_REPO, RELEASE_TAG and DB_NAME in wrangler.toml (or --var).

const PACKAGE_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]*\.pkg\.tar\.[A-Za-z0-9]+$/;
const PACKAGE_SIG_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]*\.pkg\.tar\.[A-Za-z0-9]+\.sig$/;
const DB_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]*\.db$/;

function isAllowed(path, dbName) {
  // Flat layout only: reject subdirectories and any traversal.
  if (!path || path.includes("/") || path.includes("\\")) {
    return false;
  }

  if (path === "repo.gpg") {
    return true;
  }

  if (dbName) {
    if (path === dbName || path === dbName + ".sig") {
      return true;
    }
  } else if (DB_RE.test(path) || (path.endsWith(".sig") && DB_RE.test(path.slice(0, -4)))) {
    return true;
  }

  return PACKAGE_RE.test(path) || PACKAGE_SIG_RE.test(path);
}

function isImmutable(path) {
  return PACKAGE_RE.test(path) || PACKAGE_SIG_RE.test(path);
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed\n", { status: 405 });
    }

    let path;
    try {
      path = decodeURIComponent(new URL(request.url).pathname).replace(/^\/+/, "");
    } catch {
      return new Response("bad request\n", { status: 400 });
    }

    if (!isAllowed(path, env.DB_NAME)) {
      return new Response("not found\n", { status: 404 });
    }

    const immutable = isImmutable(path);
    const hasRange = request.headers.has("range");
    const cache = caches.default;
    const cacheKey = new Request(new URL(request.url).toString(), { method: "GET" });

    if (request.method === "GET" && !hasRange) {
      const cached = await cache.match(cacheKey);
      if (cached) {
        return cached;
      }
    }

    const target =
      `https://github.com/${env.GITHUB_REPO}/releases/download/` +
      `${env.RELEASE_TAG}/${path}`;

    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("cookie");

    const origin = await fetch(target, {
      method: request.method,
      headers,
      redirect: "follow",
    });

    const outHeaders = new Headers(origin.headers);
    outHeaders.set(
      "Cache-Control",
      immutable ? "public, max-age=31536000, immutable" : "no-store",
    );
    outHeaders.delete("set-cookie");

    const response = new Response(origin.body, {
      status: origin.status,
      statusText: origin.statusText,
      headers: outHeaders,
    });

    if (immutable && request.method === "GET" && !hasRange && origin.status === 200) {
      // Caching is best-effort: very large assets may exceed the cache limit,
      // in which case we simply stream them.
      ctx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
    }

    return response;
  },
};
