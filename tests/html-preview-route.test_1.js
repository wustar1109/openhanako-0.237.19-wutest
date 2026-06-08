import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { createHtmlPreviewRoute } from "../server/routes/html-preview.js";

function makeApp(options = {}) {
  const app = new Hono();
  app.route("", createHtmlPreviewRoute(options));
  return app;
}

describe("HTML preview route", () => {
  it("serves registered HTML with a dedicated CDN-capable CSP and no referrer leakage", async () => {
    const app = makeApp({
      randomId: () => "pv_test",
      randomToken: () => "preview_secret",
      now: () => 1000,
    });
    const html = '<script src="https://cdn.tailwindcss.com"></script><h1 class="text-red-500">Hello</h1>';

    const register = await app.request("http://127.0.0.1:14500/api/preview/html", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "demo.html", content: html }),
    });

    expect(register.status).toBe(200);
    const registered = await register.json();
    expect(registered.previewUrl).toBe("http://127.0.0.1:14500/preview/html/pv_test?previewToken=preview_secret");

    const rendered = await app.request(registered.previewUrl);

    expect(rendered.status).toBe(200);
    expect(rendered.headers.get("Content-Type")).toContain("text/html");
    expect(rendered.headers.get("Cache-Control")).toBe("no-store");
    expect(rendered.headers.get("Referrer-Policy")).toBe("no-referrer");
    expect(rendered.headers.get("X-Content-Type-Options")).toBe("nosniff");
    const csp = rendered.headers.get("Content-Security-Policy") || "";
    expect(csp).toContain("script-src 'unsafe-inline' https:");
    expect(csp).toContain("style-src 'unsafe-inline' https:");
    expect(csp).toContain("font-src https: data:");
    expect(csp).toContain("img-src https: data: blob:");
    expect(csp).toContain("connect-src 'none'");
    expect(csp).not.toContain("script-src 'self'");
    expect(await rendered.text()).toBe(html);
  });

  it("requires the per-preview token and expires entries from memory", async () => {
    let now = 1000;
    const app = makeApp({
      randomId: () => "pv_expiring",
      randomToken: () => "preview_secret",
      now: () => now,
      ttlMs: 10,
    });

    const register = await app.request("http://127.0.0.1:14500/api/preview/html", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "demo.html", content: "<h1>Hello</h1>" }),
    });
    const { previewUrl } = await register.json();

    expect((await app.request("http://127.0.0.1:14500/preview/html/pv_expiring?previewToken=wrong")).status).toBe(404);
    expect((await app.request(previewUrl)).status).toBe(200);

    now = 1011;
    expect((await app.request(previewUrl)).status).toBe(404);
  });

  it("rejects oversized preview bodies before storing them", async () => {
    const app = makeApp({
      randomId: () => "pv_large",
      randomToken: () => "preview_secret",
      maxContentBytes: 8,
    });

    const register = await app.request("http://127.0.0.1:14500/api/preview/html", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "demo.html", content: "<h1>too large</h1>" }),
    });

    expect(register.status).toBe(413);
    expect(await register.json()).toEqual({ error: "html_preview_too_large" });
  });
});
