// Cloudflare Worker that fronts the pacman repository stored as GitHub release
// assets and renders a small homepage.
//
// SECURITY: this is intentionally NOT an open proxy.  Only these paths are
// served:
//   * the repository database           <DB_NAME> and <DB_NAME>.sig
//   * package files and signatures      *.pkg.tar.<ext> [.sig]
//   * the repository public key         repo.gpg
// Everything else returns 404, so the domain cannot be used to proxy arbitrary
// content (which could get it flagged for phishing).
//
// The Worker also *never* passes through a GitHub page: if the asset does not
// exist (GitHub answers with its own HTML 404) or the upstream response is
// anything other than a real file, a minimal plain-text error is returned
// instead, and GitHub/Fastly identifying headers are stripped.
//
// `/` is rendered by the Worker itself (never proxied).  The static parts of
// the page (layout, CSS, install instructions) are hardcoded below; only the
// package list (packages.json) and the last-run status (status.json) are read
// from the release.
//
// Package files are immutable and cached at the edge for a year; the database
// and the public key are always fetched fresh.
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

// Cache package files for a finite time so that any entry we fail to purge
// expires on its own; publish.py additionally purges by Cache-Tag.
const PACKAGE_TTL_SECONDS = 2592000; // 30 days

function packageTag(path) {
  const base = path.endsWith(".sig") ? path.slice(0, -4) : path;
  return `pkg:${base}`;
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

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Fetch a JSON asset from the release.  Returns the text, or null if it is
// missing or is not a plain data response (never returns a GitHub HTML page).
async function releaseAssetText(env, name) {
  const url =
    `https://github.com/${env.GITHUB_REPO}/releases/download/` +
    `${env.RELEASE_TAG}/${name}`;
  try {
    const res = await fetch(url, {
      redirect: "follow",
      headers: { "user-agent": "aur-homepage" },
    });
    if (!res.ok || (res.headers.get("content-type") || "").includes("text/html")) {
      return null;
    }
    return await res.text();
  } catch {
    return null;
  }
}

function humanTime(iso) {
  const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : (iso || "?");
}

function homePage({ origin, title, section, packages, signed, keyId }) {
  const conf =
    `[${section}]\n` +
    `SigLevel = ${signed ? "Required DatabaseOptional" : "Optional TrustAll"}\n` +
    `Server = ${origin}`;

  const keySteps = signed
    ? `<p>导入签名密钥：</p><pre>curl -fsSL ${esc(origin)}/repo.gpg -o /tmp/repo.gpg
sudo pacman-key --add /tmp/repo.gpg
sudo pacman-key --lsign-key ${esc(keyId || "<KEYID>")}</pre>`
    : "";

  const rows = packages.length
    ? packages.map((p) => {
        const label = p.status === "failed" ? "失败" : p.status === "blocked" ? "已锁定" : "正常";
        const cell = p.filename
          ? `<a href="${esc(origin)}/${esc(p.filename)}">${esc(p.filename)}</a>`
          : esc(p.name || "");
        const when = p.updated_at
          ? `<time datetime="${esc(p.updated_at)}">${esc(humanTime(p.updated_at))}</time> · `
          : "";
        return `<tr><td>${cell}</td><td>${when}${esc(label)}</td></tr>`;
      }).join("\n")
    : `<tr><td>暂无软件包</td><td></td></tr>`;

  return `<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${esc(title)}</title>
<style>
body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;line-height:1.5}
pre{background:#f4f4f4;padding:.6rem .8rem;overflow-x:auto}
table{border-collapse:collapse}
td{padding:.25rem 0;vertical-align:top}
td+td{padding-left:1.5rem;color:#666;white-space:nowrap}
</style>
</head>
<body>
<h1>${esc(title)}</h1>
<pre>${esc(conf)}</pre>
${keySteps}
<h2>软件包 (${packages.length})</h2>
<table>
<tbody>
${rows}
</tbody>
</table>
<script>
for (const t of document.querySelectorAll("time[datetime]")) {
  const d = new Date(t.getAttribute("datetime"));
  if (!isNaN(d)) {
    t.textContent = d.toLocaleString(undefined, {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  }
}
</script>
</body>
</html>`;
}

async function renderHome(request, env, ctx) {
  if (request.method === "HEAD") {
    return new Response(null, {
      headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=60" },
    });
  }

  const origin = new URL(request.url).origin;
  const section = (env.DB_NAME || "repo.db").replace(/\.db$/, "");
  const title = env.TITLE || section;

  const cache = caches.default;
  const cacheKey = new Request(origin + "/", { method: "GET" });
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }

  const text = await releaseAssetText(env, "repo.json");
  let packages = [];
  let signed = false;
  let keyId = "";
  if (text) {
    try {
      const data = JSON.parse(text);
      packages = Array.isArray(data.packages) ? data.packages : [];
      signed = !!data.signed;
      keyId = data.key_id || "";
    } catch {
      // ignore malformed manifest
    }
  }

  const html = homePage({ origin, title, section, packages, signed, keyId });
  const response = new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "public, max-age=60",
      "x-content-type-options": "nosniff",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, response.clone()).catch(() => {}));
  return response;
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

    // The homepage is rendered here, never proxied.
    if (path === "" || path === "index.html") {
      return renderHome(request, env, ctx);
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
      immutable
        ? `public, max-age=${PACKAGE_TTL_SECONDS}, immutable`
        : "no-store",
    );
    if (immutable) {
      // Tag cached package files so publish.py can purge them from Cloudflare
      // when the version is replaced or the package is removed.  Cloudflare
      // strips this header before returning the response to clients.
      outHeaders.set("Cache-Tag", `pkg,${packageTag(path)}`);
    }

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
