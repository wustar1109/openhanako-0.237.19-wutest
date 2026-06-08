import { describe, expect, it, vi } from "vitest";
import { Hono } from "hono";
import { createInfiniteCanvasProxyRoute } from "../server/infinite-canvas/proxy-route.js";

describe("Infinite-Canvas HTTP proxy route", () => {
  it("rewrites scoped paths and strips OpenHanako credentials", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("ok", { status: 201, headers: { "content-type": "text/plain" } }),
    );
    const app = new Hono();
    app.route("/api/infinite-canvas", createInfiniteCanvasProxyRoute({
      repoRoot: process.cwd(),
      ensureServiceUrl: async () => "http://127.0.0.1:4567",
    }));

    const res = await app.fetch(new Request("http://hana.local/api/infinite-canvas/api/config?token=secret&q=1", {
      method: "POST",
      headers: {
        authorization: "Bearer openhanako",
        cookie: "hana=secret",
        "content-type": "text/plain",
      },
      body: "body",
    }));

    expect(res.status).toBe(201);
    expect(await res.text()).toBe("ok");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:4567/api/config?q=1");
    expect(init.method).toBe("POST");
    expect(init.headers.get("authorization")).toBeNull();
    expect(init.headers.get("cookie")).toBeNull();
    expect(init.headers.get("content-type")).toBe("text/plain");
    fetchMock.mockRestore();
  });

  it("returns 503 when the internal service is unavailable", async () => {
    const app = new Hono();
    app.route("/api/infinite-canvas", createInfiniteCanvasProxyRoute({
      repoRoot: process.cwd(),
      ensureServiceUrl: async () => null,
    }));

    const res = await app.fetch(new Request("http://hana.local/api/infinite-canvas/api/config"));

    expect(res.status).toBe(503);
    await expect(res.json()).resolves.toMatchObject({ error: "infinite_canvas_not_ready" });
  });
});
