import crypto from "crypto";
import { Hono } from "hono";

const DEFAULT_TTL_MS = 10 * 60 * 1000;
const DEFAULT_MAX_CONTENT_BYTES = 2 * 1024 * 1024;

export const HTML_PREVIEW_CSP = [
  "default-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "object-src 'none'",
  "connect-src 'none'",
  "script-src 'unsafe-inline' https:",
  "style-src 'unsafe-inline' https:",
  "font-src https: data:",
  "img-src https: data: blob:",
  "media-src https: data: blob:",
  "frame-ancestors 'self' file: http://127.0.0.1:* http://localhost:*",
].join("; ");

export function createHtmlPreviewRoute({
  ttlMs = DEFAULT_TTL_MS,
  maxContentBytes = DEFAULT_MAX_CONTENT_BYTES,
  now = () => Date.now(),
  randomId = () => `pv_${crypto.randomBytes(16).toString("hex")}`,
  randomToken = () => crypto.randomBytes(32).toString("base64url"),
} = {}) {
  const route = new Hono();
  const previews = new Map();

  route.post("/api/preview/html", async (c) => {
    cleanupExpired(previews, now());

    let body;
    try {
      body = await c.req.json();
    } catch {
      return c.json({ error: "invalid_json" }, 400);
    }

    const content = typeof body?.content === "string" ? body.content : null;
    if (content === null) return c.json({ error: "missing_content" }, 400);
    if (Buffer.byteLength(content, "utf-8") > maxContentBytes) {
      return c.json({ error: "html_preview_too_large" }, 413);
    }

    const id = randomId();
    const token = randomToken();
    const expiresAt = now() + ttlMs;
    previews.set(id, {
      token,
      content,
      title: typeof body?.title === "string" ? body.title.slice(0, 240) : "",
      expiresAt,
    });

    const requestUrl = new URL(c.req.url);
    const previewUrl = new URL(`/preview/html/${encodeURIComponent(id)}`, requestUrl.origin);
    previewUrl.searchParams.set("previewToken", token);

    return c.json({
      id,
      previewUrl: previewUrl.toString(),
      expiresAt,
    });
  });

  route.get("/preview/html/:id", (c) => servePreview(c, previews, now()));
  route.on("HEAD", "/preview/html/:id", (c) => servePreview(c, previews, now(), true));

  return route;
}

function servePreview(c, previews, currentTime, headOnly = false) {
  cleanupExpired(previews, currentTime);

  const id = c.req.param("id");
  const token = c.req.query("previewToken") || "";
  const preview = previews.get(id);
  if (!preview || preview.token !== token) {
    return c.body(null, 404);
  }

  c.header("Content-Type", "text/html; charset=utf-8");
  c.header("Content-Security-Policy", HTML_PREVIEW_CSP);
  c.header("Referrer-Policy", "no-referrer");
  c.header("X-Content-Type-Options", "nosniff");
  c.header("Cache-Control", "no-store");
  c.header("Cross-Origin-Resource-Policy", "cross-origin");

  return c.body(headOnly ? null : preview.content);
}

function cleanupExpired(previews, currentTime) {
  for (const [id, preview] of previews.entries()) {
    if (preview.expiresAt <= currentTime) previews.delete(id);
  }
}
