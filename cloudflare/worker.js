// Cloudflare Worker that fronts a pacman repository stored as GitHub release
// assets, adding a stable URL and edge caching.
//
// Every request `/<file>` is mapped to
//   https://github.com/<GITHUB_REPO>/releases/download/<RELEASE_TAG>/<file>
// GitHub redirects that to its object storage, which the Worker follows.  The
// result is cached at the edge: package files are immutable and cached for a
// year, while the repository database is cached only briefly.
//
// Configure GITHUB_REPO and RELEASE_TAG in wrangler.toml (or via --var).

function isImmutable(path) {
  return (
    /\.pkg\.tar\.(zst|xz|gz|bz2|lz4|lzo|lz)$/.test(path) ||
    /\.pkg\.tar\.[^.]+\.sig$/.test(path)
  );
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    let path;
    try {
      path = decodeURIComponent(url.pathname).replace(/^\/+/, "");
    } catch {
      return new Response("bad request\n", { status: 400 });
    }

    if (path === "") {
      return new Response("AUR repository proxy\n", { status: 200 });
    }
    if (path.includes("..")) {
      return new Response("bad request\n", { status: 400 });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed\n", { status: 405 });
    }

    const immutable = isImmutable(path);
    const hasRange = request.headers.has("range");
    const cache = caches.default;
    const cacheKey = new Request(url.toString(), { method: "GET" });

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
      immutable
        ? "public, max-age=31536000, immutable"
        : "no-store",
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
