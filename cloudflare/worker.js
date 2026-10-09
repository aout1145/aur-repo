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
// The Worker also *never* passes through a GitHub page.  If the asset does not
// exist (GitHub returns its own HTML 404) or the upstream response is anything
// other than a real file, a minimal plain-text error is returned instead, and
// GitHub/Fastly identifying headers are stripped from successful responses.
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

function errorResponse(status, message) {
  return new Response(message + "\n", {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

// Remove anything that would reveal (or cache) the GitHub/Fastly origin.
function sanitizeHeaders(headers) {
  for (const key of [...headers.keys()]) {
    const k = key.toLowerCase();
    if (
      k === "set-cookie" ||
      k === "via" ||
      k === "server" ||
      k.startsWith("x-github-") ||
      k.startsWith("x-fastly-") ||
      k.startsWith("x-served-by") ||
      k.startsWith("x-cache") ||
      k.startsWith("x-timer") ||
      k.startsWith("x-ratelimit-") ||
      k.startsWith("content-security-policy")
    ) {
      headers.delete(key);
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return errorResponse(405, "method not allowed");
    }

    let path;
    try {
      path = decodeURIComponent(new URL(request.url).pathname).replace(/^\/+/, "");
    } catch {
      return errorResponse(400, "bad request");
    }

    if (!isAllowed(path, env.DB_NAME)) {
      return errorResponse(404, "not found");
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

    let origin;
    try {
      origin = await fetch(target, {
        method: request.method,
        headers,
        redirect: "follow",
      });
    } catch {
      return errorResponse(502, "bad gateway");
    }

    const contentType = origin.headers.get("content-type") || "";
    const ok = origin.status === 200 || origin.status === 206;

    // Never let a GitHub error page (or any HTML) reach the client.
    if (!ok || contentType.includes("text/html")) {
      return errorResponse(origin.status === 404 ? 404 : 502,
        origin.status === 404 ? "not found" : "bad gateway");
    }

    const outHeaders = new Headers(origin.headers);
    sanitizeHeaders(outHeaders);
    outHeaders.set(
      "Cache-Control",
      immutable ? "public, max-age=31536000, immutable" : "no-store",
    );

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
