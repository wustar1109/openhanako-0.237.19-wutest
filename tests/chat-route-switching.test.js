import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_DISCONNECT_ABORT_GRACE_MS,
  createChatRoute,
  resolveDisconnectAbortGraceMs,
} from "../server/routes/chat.js";

describe("chat route model switch guard", () => {
  it("uses a minute-scale default WS disconnect abort grace and allows disabling it", () => {
    expect(DEFAULT_DISCONNECT_ABORT_GRACE_MS).toBeGreaterThanOrEqual(60_000);
    expect(resolveDisconnectAbortGraceMs(undefined)).toBe(DEFAULT_DISCONNECT_ABORT_GRACE_MS);
    expect(resolveDisconnectAbortGraceMs("0")).toBe(0);
    expect(resolveDisconnectAbortGraceMs("45000")).toBe(45_000);
    expect(resolveDisconnectAbortGraceMs("-1")).toBe(DEFAULT_DISCONNECT_ABORT_GRACE_MS);
    expect(resolveDisconnectAbortGraceMs("bad")).toBe(DEFAULT_DISCONNECT_ABORT_GRACE_MS);
  });

  it("rejects prompts through the engine public switching API", async () => {
    let createHandlers;
    const upgradeWebSocket = vi.fn((factory) => {
      createHandlers = factory;
      return () => new Response(null);
    });
    const hub = {
      subscribe: vi.fn(),
      send: vi.fn(async () => {}),
    };
    const engine = {
      agentName: "Hana",
      abortAllStreaming: vi.fn(async () => {}),
      getSessionByPath: vi.fn(() => null),
      isSessionStreaming: vi.fn(() => false),
      isSessionSwitching: vi.fn(() => true),
      steerSession: vi.fn(() => false),
      slashDispatcher: null,
    };

    createChatRoute(engine, hub, { upgradeWebSocket });
    const handlers = createHandlers({});
    const ws = {
      readyState: 1,
      send: vi.fn(),
    };

    handlers.onMessage({
      data: JSON.stringify({
        type: "prompt",
        text: "hello",
        sessionPath: "/tmp/session.jsonl",
      }),
    }, ws);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(engine.isSessionSwitching).toHaveBeenCalledWith("/tmp/session.jsonl");
    expect(hub.send).not.toHaveBeenCalled();
    expect(JSON.parse(ws.send.mock.calls[0][0])).toMatchObject({
      type: "error",
      message: "正在切换模型，请稍候",
      sessionPath: "/tmp/session.jsonl",
    });
  });

  it("keeps remote and host clients on the same server-side session stream", async () => {
    let createHandlers;
    let subscriber;
    const upgradeWebSocket = vi.fn((factory) => {
      createHandlers = factory;
      return () => new Response(null);
    });
    const hub = {
      subscribe: vi.fn((fn) => {
        subscriber = fn;
      }),
      send: vi.fn(async () => {}),
    };
    const engine = {
      agentName: "Hana",
      abortAllStreaming: vi.fn(async () => {}),
      getSessionByPath: vi.fn(() => ({ entries: [] })),
      isSessionStreaming: vi.fn(() => false),
      isSessionSwitching: vi.fn(() => false),
      steerSession: vi.fn(() => false),
      slashDispatcher: null,
    };

    createChatRoute(engine, hub, { upgradeWebSocket });
    const handlers = createHandlers({});
    const hostWs = { readyState: 1, send: vi.fn() };
    const phoneWs = { readyState: 1, send: vi.fn() };
    handlers.onOpen({}, hostWs);
    handlers.onOpen({}, phoneWs);

    handlers.onMessage({
      data: JSON.stringify({
        type: "prompt",
        text: "hello from phone",
        sessionPath: "/tmp/shared-session.jsonl",
      }),
    }, phoneWs);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(hub.send).toHaveBeenCalledWith("hello from phone", expect.objectContaining({
      sessionPath: "/tmp/shared-session.jsonl",
    }));

    subscriber?.({
      type: "session_user_message",
      message: { id: "u1", text: "hello from phone" },
    }, "/tmp/shared-session.jsonl");

    for (const ws of [hostWs, phoneWs]) {
      expect(ws.send).toHaveBeenCalledWith(expect.any(String));
      const payloads = ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
      expect(payloads).toEqual(expect.arrayContaining([
        expect.objectContaining({
          type: "session_user_message",
          sessionPath: "/tmp/shared-session.jsonl",
          message: { id: "u1", text: "hello from phone" },
        }),
      ]));
    }

    handlers.onClose({}, hostWs);
    handlers.onClose({}, phoneWs);
  });

  it("emits file content blocks for deferred result session files", () => {
    let createHandlers;
    let subscriber;
    const upgradeWebSocket = vi.fn((factory) => {
      createHandlers = factory;
      return () => new Response(null);
    });
    const hub = {
      subscribe: vi.fn((fn) => {
        subscriber = fn;
      }),
      send: vi.fn(async () => {}),
    };
    const engine = {
      agentName: "Hana",
      abortAllStreaming: vi.fn(async () => {}),
      getSessionByPath: vi.fn(() => ({ entries: [] })),
      isSessionStreaming: vi.fn(() => false),
      isSessionSwitching: vi.fn(() => false),
      steerSession: vi.fn(() => false),
      slashDispatcher: null,
    };

    createChatRoute(engine, hub, { upgradeWebSocket });
    const handlers = createHandlers({});
    const ws = { readyState: 1, send: vi.fn() };
    handlers.onOpen({}, ws);

    subscriber?.({
      type: "deferred_result",
      taskId: "img-task-1",
      status: "success",
      result: {
        files: ["abc.png"],
        sessionFiles: [{
          fileId: "sf_generated",
          filePath: "/tmp/generated/abc.png",
          label: "abc.png",
          ext: "png",
          mime: "image/png",
          kind: "image",
          storageKind: "plugin_data",
          status: "available",
        }],
      },
      meta: { type: "image-generation" },
    }, "/tmp/image-session.jsonl");

    const payloads = ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: "deferred_result",
        sessionPath: "/tmp/image-session.jsonl",
        taskId: "img-task-1",
        status: "success",
      }),
      expect.objectContaining({
        type: "content_block",
        sessionPath: "/tmp/image-session.jsonl",
        block: expect.objectContaining({
          type: "file",
          fileId: "sf_generated",
          filePath: "/tmp/generated/abc.png",
          label: "abc.png",
          ext: "png",
          mime: "image/png",
          kind: "image",
          storageKind: "plugin_data",
          status: "available",
        }),
      }),
    ]));

    handlers.onClose({}, ws);
  });

  it("broadcasts browser_status events emitted outside tool execution", () => {
    let createHandlers;
    let subscriber;
    const upgradeWebSocket = vi.fn((factory) => {
      createHandlers = factory;
      return () => new Response(null);
    });
    const hub = {
      subscribe: vi.fn((fn) => {
        subscriber = fn;
      }),
      send: vi.fn(async () => {}),
    };
    const engine = {
      agentName: "Hana",
      abortAllStreaming: vi.fn(async () => {}),
      getSessionByPath: vi.fn(() => ({ entries: [] })),
      isSessionStreaming: vi.fn(() => false),
      isSessionSwitching: vi.fn(() => false),
      steerSession: vi.fn(() => false),
      slashDispatcher: null,
    };

    createChatRoute(engine, hub, { upgradeWebSocket });
    const handlers = createHandlers({});
    const ws = { readyState: 1, send: vi.fn() };
    handlers.onOpen({}, ws);

    subscriber?.({
      type: "browser_status",
      running: false,
      url: null,
    }, "/tmp/browser-session.jsonl");

    const payloads = ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
    expect(payloads).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: "browser_status",
        running: false,
        url: null,
        sessionPath: "/tmp/browser-session.jsonl",
      }),
    ]));

    handlers.onClose({}, ws);
  });

  it("does not serialize broadcast payloads for closed clients", () => {
    let createHandlers;
    let subscriber;
    const upgradeWebSocket = vi.fn((factory) => {
      createHandlers = factory;
      return () => new Response(null);
    });
    const hub = {
      subscribe: vi.fn((fn) => {
        subscriber = fn;
      }),
      send: vi.fn(async () => {}),
    };
    const engine = {
      agentName: "Hana",
      abortAllStreaming: vi.fn(async () => {}),
      getSessionByPath: vi.fn(() => ({ entries: [] })),
      isSessionStreaming: vi.fn(() => false),
      isSessionSwitching: vi.fn(() => false),
      steerSession: vi.fn(() => false),
      slashDispatcher: null,
    };

    createChatRoute(engine, hub, { upgradeWebSocket });
    const handlers = createHandlers({});
    const closedWs = { readyState: 3, send: vi.fn() };
    handlers.onOpen({}, closedWs);

    const toxicSession = {
      toJSON() {
        throw new Error("closed clients must not force serialization");
      },
    };

    expect(() => {
      subscriber?.({
        type: "session_created",
        session: toxicSession,
      }, "/tmp/closed-client-session.jsonl");
    }).not.toThrow();
    expect(closedWs.send).not.toHaveBeenCalled();

    handlers.onClose({}, closedWs);
  });
});
