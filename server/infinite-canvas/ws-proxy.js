import { Hono } from "hono";
import WebSocket from "ws";
import {
  getInfiniteCanvasServiceUrl,
  startInfiniteCanvasService,
} from "./service.js";

const WS_PREFIX = "/ws/infinite-canvas";

function upstreamWsUrl(requestUrl, serviceUrl) {
  const incoming = new URL(requestUrl);
  let path = incoming.pathname.startsWith(WS_PREFIX)
    ? incoming.pathname.slice(WS_PREFIX.length)
    : incoming.pathname;
  if (!path) path = "/";
  if (!path.startsWith("/")) path = `/${path}`;
  const search = new URLSearchParams(incoming.search);
  search.delete("token");
  const base = new URL(serviceUrl);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.pathname = `/ws${path}`;
  base.search = search.toString();
  return base.toString();
}

async function ensureServiceUrl() {
  return getInfiniteCanvasServiceUrl() || await startInfiniteCanvasService();
}

export function createInfiniteCanvasWsProxyRoute({ upgradeWebSocket, ensureServiceUrl: ensureUrl = ensureServiceUrl } = {}) {
  const route = new Hono();

  route.get("/ws/infinite-canvas/*", upgradeWebSocket((c) => {
    const requestUrl = c.req.url;
    let upstream = null;
    let upstreamOpen = false;
    let clientClosed = false;
    const pending = [];

    function closePair(ws, code = 1000, reason = "") {
      clientClosed = true;
      try { ws.close(code, reason); } catch {}
      try { upstream?.close(code, reason); } catch {}
    }

    return {
      onOpen(_event, ws) {
        (async () => {
          const serviceUrl = await ensureUrl();
          if (!serviceUrl) {
            closePair(ws, 1013, "Infinite Canvas service not ready");
            return;
          }
          if (clientClosed) return;
          upstream = new WebSocket(upstreamWsUrl(requestUrl, serviceUrl), {
            headers: {},
          });
          upstream.on("open", () => {
            upstreamOpen = true;
            while (pending.length > 0) upstream.send(pending.shift());
          });
          upstream.on("message", (data, isBinary) => {
            try { ws.send(data, { binary: isBinary }); } catch {}
          });
          upstream.on("close", (code, reason) => {
            closePair(ws, code || 1000, reason?.toString?.() || "");
          });
          upstream.on("error", () => {
            closePair(ws, 1011, "Infinite Canvas upstream error");
          });
        })().catch(() => closePair(ws, 1011, "Infinite Canvas proxy error"));
      },
      onMessage(event) {
        if (!upstream || !upstreamOpen) {
          pending.push(event.data);
          return;
        }
        upstream.send(event.data);
      },
      onClose() {
        clientClosed = true;
        try { upstream?.close(); } catch {}
      },
      onError(_event, ws) {
        closePair(ws, 1011, "Infinite Canvas client error");
      },
    };
  }));

  return route;
}
