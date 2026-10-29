const { createReadStream, existsSync, statSync } = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const CONTENT_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gif", "image/gif"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".map", "application/json; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".txt", "text/plain; charset=utf-8"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"],
]);

const SECURITY_HEADERS = {
  "Content-Security-Policy":
    "default-src 'self'; base-uri 'self'; object-src 'none'; " +
    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data: blob: https:; font-src 'self' data: https:; " +
    "connect-src 'self' http://localhost:* http://127.0.0.1:* " +
    "ws://localhost:* ws://127.0.0.1:* https: wss:; " +
    "media-src 'self' data: blob: http: https:; worker-src 'self' blob:; " +
    "frame-src 'self' http: https:; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
};

/**
 * Resolve a request path inside a static-export root without allowing traversal.
 *
 * @param {string} rootDirectory - Static export directory.
 * @param {string} pathname - URL pathname from the loopback request.
 * @returns {string | undefined} An existing file path or undefined for a miss.
 */
function resolveStaticFile(rootDirectory, pathname) {
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(pathname);
  } catch {
    return undefined;
  }

  const root = path.resolve(rootDirectory);
  const relativePath =
    decodedPath === "/" ? "index.html" : decodedPath.replace(/^\/+/, "");
  const candidate = path.resolve(root, relativePath);
  if (candidate !== root && !candidate.startsWith(`${root}${path.sep}`))
    return undefined;

  if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
  const routePath = relativePath.replace(/\/+$/, "");
  if (routePath && !path.extname(routePath)) {
    const flatExport = path.join(root, `${routePath}.html`);
    if (existsSync(flatExport) && statSync(flatExport).isFile()) return flatExport;
  }
  if (existsSync(candidate) && statSync(candidate).isDirectory()) {
    const indexFile = path.join(candidate, "index.html");
    if (existsSync(indexFile) && statSync(indexFile).isFile()) return indexFile;
  }

  // Next's static export uses an index page for client routes. Only use the
  // fallback for paths that are not already requesting a concrete asset.
  if (!path.extname(relativePath)) {
    const fallback = path.join(root, "index.html");
    if (existsSync(fallback) && statSync(fallback).isFile()) return fallback;
  }
  return undefined;
}

function contentType(filePath) {
  return (
    CONTENT_TYPES.get(path.extname(filePath).toLowerCase()) ||
    "application/octet-stream"
  );
}

/**
 * Serve a Next static export from a loopback-only HTTP server.
 *
 * @param {{rootDirectory: string, host?: string, port?: number}} options
 * @returns {Promise<{url: string, close: () => Promise<void>}>}
 */
function createLocalFrontendServer({
  rootDirectory,
  host = "127.0.0.1",
  port = 0,
}) {
  if (!existsSync(rootDirectory) || !statSync(rootDirectory).isDirectory()) {
    return Promise.reject(
      new Error(`The desktop frontend export is missing: ${rootDirectory}`),
    );
  }

  const server = http.createServer((request, response) => {
    if (request.method !== "GET" && request.method !== "HEAD") {
      response.writeHead(405, { Allow: "GET, HEAD" });
      response.end();
      return;
    }

    const requestUrl = new URL(request.url || "/", `http://${host}`);
    const filePath = resolveStaticFile(rootDirectory, requestUrl.pathname);
    if (!filePath) {
      const notFound = path.join(rootDirectory, "404.html");
      if (existsSync(notFound) && statSync(notFound).isFile()) {
        response.writeHead(404, {
          ...SECURITY_HEADERS,
          "Content-Type": contentType(notFound),
        });
        if (request.method === "HEAD") response.end();
        else createReadStream(notFound).pipe(response);
        return;
      }
      response.writeHead(404, {
        ...SECURITY_HEADERS,
        "Content-Type": "text/plain; charset=utf-8",
      });
      response.end("Not found");
      return;
    }

    response.writeHead(200, {
      ...SECURITY_HEADERS,
      "Cache-Control": "no-store",
      "Content-Type": contentType(filePath),
    });
    if (request.method === "HEAD") response.end();
    else createReadStream(filePath).pipe(response);
  });

  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.removeListener("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.removeListener("error", onError);
      const address = server.address();
      if (!address || typeof address === "string") {
        reject(
          new Error("The local frontend server did not provide a TCP address."),
        );
        return;
      }
      resolve({
        url: `http://${host}:${address.port}`,
        close: () =>
          new Promise((closeResolve, closeReject) => {
            server.close((error) =>
              error ? closeReject(error) : closeResolve(),
            );
          }),
      });
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, host);
  });
}

module.exports = { createLocalFrontendServer, resolveStaticFile };
