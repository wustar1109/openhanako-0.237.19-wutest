import fs from "fs";
import path from "path";
import { Hono } from "hono";
import { fromRoot } from "../../shared/hana-root.js";
import {
  getInfiniteCanvasServiceError,
  getInfiniteCanvasServiceUrl,
  startInfiniteCanvasService,
} from "./service.js";

const PROXY_PREFIX = "/api/infinite-canvas";
const HOP_BY_HOP_HEADERS = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "authorization",
  "cookie",
];

function stripHeaders(headers) {
  const next = new Headers(headers);
  for (const name of HOP_BY_HOP_HEADERS) next.delete(name);
  return next;
}

function responseHeaders(headers) {
  const next = new Headers(headers);
  for (const name of HOP_BY_HOP_HEADERS) next.delete(name);
  return next;
}

function upstreamPathFor(url) {
  let subPath = url.pathname.startsWith(PROXY_PREFIX)
    ? url.pathname.slice(PROXY_PREFIX.length)
    : url.pathname;
  if (!subPath) subPath = "/";
  if (!subPath.startsWith("/")) subPath = `/${subPath}`;
  const search = new URLSearchParams(url.search);
  search.delete("token");
  const qs = search.toString();
  return `${subPath}${qs ? `?${qs}` : ""}`;
}

function listStaticHtmlPages(repoRoot = fromRoot()) {
  const staticDir = path.join(repoRoot, "third_party", "Infinite-Canvas", "static");
  try {
    return fs.readdirSync(staticDir)
      .filter(file => file.toLowerCase().endsWith(".html"))
      .sort((a, b) => a.localeCompare(b));
  } catch {
    return [];
  }
}

async function ensureServiceUrl() {
  return getInfiniteCanvasServiceUrl() || await startInfiniteCanvasService();
}

export function createInfiniteCanvasProxyRoute(options = {}) {
  const route = new Hono();
  const repoRoot = options.repoRoot || fromRoot();
  const ensureUrl = options.ensureServiceUrl || ensureServiceUrl;

  route.get("/openhanako/static-pages", (c) => {
    return c.json({ pages: listStaticHtmlPages(repoRoot) });
  });

  route.all("/*", async (c) => {
    const serviceUrl = await ensureUrl();
    if (!serviceUrl) {
      return c.json({
        error: "infinite_canvas_not_ready",
        detail: getInfiniteCanvasServiceError(),
      }, 503);
    }

    const incoming = new URL(c.req.url);
    const target = new URL(upstreamPathFor(incoming), serviceUrl);
    const method = c.req.method.toUpperCase();
    const hasBody = method !== "GET" && method !== "HEAD";
    const init = {
      method,
      headers: stripHeaders(c.req.raw.headers),
      redirect: "manual",
    };
    if (hasBody) {
      init.body = c.req.raw.body;
      init.duplex = "half";
    }

    const res = await fetch(target, init);
    return new Response(res.body, {
      status: res.status,
      statusText: res.statusText,
      headers: responseHeaders(res.headers),
    });
  });

  return route;
}
