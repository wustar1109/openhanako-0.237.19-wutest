import { describe, it, expect, vi } from "vitest";
import { MCP_PROTOCOL_VERSION } from "../plugins/mcp/lib/mcp-stdio-client.js";
import {
  McpAutoHttpClient,
  McpLegacySseClient,
  McpStreamableHttpClient,
} from "../plugins/mcp/lib/mcp-http-client.js";

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
  });
}

function emptyResponse({ status = 202, headers = {} } = {}) {
  return new Response(null, { status, headers });
}

function headerValue(headers, name) {
  const lower = name.toLowerCase();
  if (headers instanceof Headers) return headers.get(name);
  const found = Object.entries(headers || {}).find(([key]) => key.toLowerCase() === lower);
  return found?.[1];
}

function requestBody(init) {
  return init?.body ? JSON.parse(String(init.body)) : null;
}

describe("MCP HTTP clients", () => {
  it("uses Streamable HTTP JSON-RPC POST with bearer auth and session headers", async () => {
    const requests = [];
    const fetchImpl = vi.fn(async (url, init) => {
      const body = requestBody(init);
      requests.push({ url: String(url), init, body });
      if (body?.method === "initialize") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
        }, { headers: { "MCP-Session-Id": "session-a" } });
      }
      if (body?.method === "notifications/initialized") return emptyResponse();
      if (body?.method === "tools/list") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { tools: [{ name: "search", inputSchema: { type: "object" } }] },
        });
      }
      if (body?.method === "tools/call") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { content: [{ type: "text", text: "ok" }] },
        });
      }
      throw new Error(`unexpected method ${body?.method}`);
    });

    const client = new McpStreamableHttpClient({
      id: "github",
      url: "https://mcp.github.com/mcp",
      authorizationToken: "token-123",
    }, { fetchImpl });

    await client.start();
    const tools = await client.listTools();
    const result = await client.callTool("search", { q: "hana" });

    expect(tools).toEqual([{ name: "search", inputSchema: { type: "object" } }]);
    expect(result.content[0].text).toBe("ok");
    expect(requests.map(r => r.body?.method)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/list",
      "tools/call",
    ]);
    expect(headerValue(requests[0].init.headers, "Accept")).toBe("application/json, text/event-stream");
    expect(headerValue(requests[0].init.headers, "Content-Type")).toBe("application/json");
    expect(headerValue(requests[0].init.headers, "Authorization")).toBe("Bearer token-123");
    expect(headerValue(requests[2].init.headers, "MCP-Protocol-Version")).toBe(MCP_PROTOCOL_VERSION);
    expect(headerValue(requests[2].init.headers, "MCP-Session-Id")).toBe("session-a");
  });

  it("sends custom connector headers while preserving protocol headers", async () => {
    const requests = [];
    const fetchImpl = vi.fn(async (url, init) => {
      const body = requestBody(init);
      requests.push({ url: String(url), init, body });
      if (body?.method === "initialize") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
        });
      }
      if (body?.method === "notifications/initialized") return emptyResponse();
      return jsonResponse({
        jsonrpc: "2.0",
        id: body.id,
        result: { tools: [] },
      });
    });

    const client = new McpStreamableHttpClient({
      id: "private",
      url: "https://mcp.example.com/mcp",
      headers: {
        Accept: "text/plain",
        "X-API-Key": "key-123",
        Authorization: "Bearer header-token",
      },
    }, { fetchImpl });

    await client.start();
    await client.listTools();

    expect(headerValue(requests[0].init.headers, "Accept")).toBe("application/json, text/event-stream");
    expect(headerValue(requests[0].init.headers, "Content-Type")).toBe("application/json");
    expect(headerValue(requests[0].init.headers, "X-API-Key")).toBe("key-123");
    expect(headerValue(requests[0].init.headers, "Authorization")).toBe("Bearer header-token");
  });

  it("reinitializes once when a Streamable HTTP session expires", async () => {
    const requests = [];
    let initializeCount = 0;
    let expiredOnce = false;
    const fetchImpl = vi.fn(async (url, init) => {
      const body = requestBody(init);
      requests.push({ url: String(url), init, body });
      if (body?.method === "initialize") {
        initializeCount += 1;
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} },
        }, { headers: { "MCP-Session-Id": `session-${initializeCount}` } });
      }
      if (body?.method === "notifications/initialized") return emptyResponse();
      if (body?.method === "tools/list" && headerValue(init.headers, "MCP-Session-Id") === "session-1" && !expiredOnce) {
        expiredOnce = true;
        return jsonResponse({ error: "expired" }, { status: 404 });
      }
      if (body?.method === "tools/list") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { tools: [{ name: "fresh" }] },
        });
      }
      throw new Error(`unexpected method ${body?.method}`);
    });

    const client = new McpStreamableHttpClient({
      id: "github",
      url: "https://mcp.github.com/mcp",
    }, { fetchImpl });

    await client.start();
    const tools = await client.listTools();

    expect(tools).toEqual([{ name: "fresh" }]);
    expect(initializeCount).toBe(2);
    const listRequests = requests.filter(r => r.body?.method === "tools/list");
    expect(headerValue(listRequests.at(-1).init.headers, "MCP-Session-Id")).toBe("session-2");
  });

  it("uses one initial Streamable HTTP protocol version source and then negotiated headers", async () => {
    const requests = [];
    const fetchImpl = vi.fn(async (url, init) => {
      const body = requestBody(init);
      requests.push({ url: String(url), init, body });
      if (body?.method === "initialize") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { protocolVersion: "2026-01-01", capabilities: {} },
        }, { headers: { "MCP-Session-Id": "session-a" } });
      }
      if (body?.method === "notifications/initialized") return emptyResponse();
      if (body?.method === "tools/list") {
        return jsonResponse({
          jsonrpc: "2.0",
          id: body.id,
          result: { tools: [] },
        });
      }
      throw new Error(`unexpected method ${body?.method}`);
    });

    const client = new McpStreamableHttpClient({
      id: "versioned",
      url: "https://mcp.example.com/mcp",
      headers: {
        "MCP-Protocol-Version": "2024-11-05",
      },
    }, { fetchImpl });

    await client.start();
    await client.listTools();

    expect(requests[0].body.params.protocolVersion).toBe("2024-11-05");
    expect(headerValue(requests[0].init.headers, "MCP-Protocol-Version")).toBe("2024-11-05");
    expect(headerValue(requests[2].init.headers, "MCP-Protocol-Version")).toBe("2026-01-01");
  });

  it("does not let a legacy SSE server ping with the same id satisfy a tool response", async () => {
    const encoder = new TextEncoder();
    let streamController;
    const fetchImpl = vi.fn(async (url, init = {}) => {
      const body = requestBody(init);
      if (init.method === "POST" && body?.method === "tools/call") {
        queueMicrotask(() => {
          streamController.enqueue(encoder.encode(`data: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, method: "ping", params: {} })}\n\n`));
          streamController.enqueue(encoder.encode(`data: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: "ok" }] } })}\n\n`));
        });
        return emptyResponse();
      }
      throw new Error(`unexpected request ${url}`);
    });
    const client = new McpLegacySseClient({
      id: "legacy",
      url: "https://legacy.example.com/sse",
    }, { fetchImpl });
    const stream = new ReadableStream({
      start(controller) {
        streamController = controller;
      },
    });
    client.messageEndpoint = "https://legacy.example.com/messages";
    client._closed = false;
    client._handleSseEvent({
      event: "message",
      data: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping", params: {} }),
    });
    client._readSse(stream).catch((err) => {
      throw err;
    });

    const result = await client.callTool("search", { q: "hana" });
    await client.stop();

    expect(result).toEqual({ content: [{ type: "text", text: "ok" }] });
    expect(client._queued.size).toBe(0);
  });

  it("falls back from Streamable HTTP to legacy SSE endpoint transport", async () => {
    const requests = [];
    const encoder = new TextEncoder();
    let sseController;

    function sendSse(event, data) {
      sseController.enqueue(encoder.encode(`event: ${event}\ndata: ${data}\n\n`));
    }

    const fetchImpl = vi.fn(async (url, init = {}) => {
      const body = requestBody(init);
      requests.push({ url: String(url), init, body });
      if (init.method === "GET") {
        const stream = new ReadableStream({
          start(controller) {
            sseController = controller;
            queueMicrotask(() => sendSse("endpoint", "/messages"));
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (String(url) === "https://legacy.example.com/sse" && body?.method === "initialize") {
        return jsonResponse({ error: "not found" }, { status: 404 });
      }
      if (String(url) === "https://legacy.example.com/messages") {
        if (body?.id != null) {
          queueMicrotask(() => {
            const result = body.method === "tools/list"
              ? { tools: [{ name: "legacy-search" }] }
              : { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: {} };
            sendSse("message", JSON.stringify({ jsonrpc: "2.0", id: body.id, result }));
          });
        }
        return emptyResponse();
      }
      throw new Error(`unexpected request ${url}`);
    });

    const client = new McpAutoHttpClient({
      id: "legacy",
      url: "https://legacy.example.com/sse",
    }, { fetchImpl });

    await client.start();
    const tools = await client.listTools();
    await client.stop();

    expect(tools).toEqual([{ name: "legacy-search" }]);
    expect(requests.map(r => `${r.init.method || "POST"} ${r.url}`)).toContain("GET https://legacy.example.com/sse");
    expect(requests.map(r => r.url)).toContain("https://legacy.example.com/messages");
  });
});
