import { beforeEach, describe, expect, it, vi } from "vitest";

const {
  completeSimpleMock,
  convertAgentMessagesToLlmMock,
  estimateTokensMock,
  findCutPointMock,
  prepareCompactionMock,
} = vi.hoisted(() => ({
  completeSimpleMock: vi.fn(),
  convertAgentMessagesToLlmMock: vi.fn(async (messages) => messages),
  estimateTokensMock: vi.fn((message) => {
    const content = Array.isArray(message?.content)
      ? message.content.map((block) => block?.text || block?.thinking || JSON.stringify(block || {})).join("")
      : String(message?.content || message?.summary || "");
    return Math.ceil(content.length / 4);
  }),
  findCutPointMock: vi.fn(() => ({ firstKeptEntryIndex: 1, turnStartIndex: -1, isSplitTurn: false })),
  prepareCompactionMock: vi.fn(),
}));

vi.mock("../lib/pi-sdk/index.js", () => ({
  completeSimple: completeSimpleMock,
  convertAgentMessagesToLlm: convertAgentMessagesToLlmMock,
  estimateTokens: estimateTokensMock,
  findCutPoint: findCutPointMock,
  prepareCompaction: prepareCompactionMock,
}));

import {
  compactSessionWithCachePreservation,
  createCachePreservingCompactionResult,
  runCachePreservingCompactionForSession,
} from "../core/session-compactor.js";

describe("session-compactor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    convertAgentMessagesToLlmMock.mockImplementation(async (messages) => messages);
    estimateTokensMock.mockImplementation((message) => {
      const content = Array.isArray(message?.content)
        ? message.content.map((block) => block?.text || block?.thinking || JSON.stringify(block || {})).join("")
        : String(message?.content || message?.summary || "");
      return Math.ceil(content.length / 4);
    });
    findCutPointMock.mockReturnValue({ firstKeptEntryIndex: 1, turnStartIndex: -1, isSplitTurn: false });
  });

  it("appends an internal compaction instruction and strips tools from the summary call", async () => {
    const signal = new AbortController().signal;
    const resultStream = {
      result: vi.fn(async () => ({
        stopReason: "stop",
        content: [{ type: "text", text: " checkpoint summary " }],
      })),
    };
    const streamFn = vi.fn(async () => resultStream);
    const convertToLlm = vi.fn(async (messages) => messages);

    const result = await createCachePreservingCompactionResult({
      preparation: {
        firstKeptEntryId: "entry-keep",
        tokensBefore: 1234,
        settings: { reserveTokens: 1000 },
        fileOps: {
          read: new Set(["/tmp/read.md", "/tmp/edited.md"]),
          written: new Set(["/tmp/written.md"]),
          edited: new Set(["/tmp/edited.md"]),
        },
      },
      model: { id: "model", reasoning: true },
      systemPrompt: "system prompt",
      messages: [{ role: "user", content: [{ type: "text", text: "hello" }], timestamp: 1 }],
      tools: [{ name: "read", description: "Read files", parameters: { type: "object" } }],
      customInstructions: "focus on decisions",
      signal,
      thinkingLevel: "high",
      streamFn,
      convertToLlm,
    });

    expect(convertToLlm).toHaveBeenCalledOnce();
    expect(streamFn).toHaveBeenCalledOnce();
    const [model, context, options] = streamFn.mock.calls[0];
    expect(model).toEqual({ id: "model", reasoning: true });
    expect(context.systemPrompt).toBe("system prompt");
    expect(context.tools).toBeUndefined();
    expect(context.messages).toHaveLength(2);
    expect(context.messages[1].role).toBe("user");
    expect(context.messages[1].content[0].text).toContain("Hana cache-preserving compaction");
    expect(context.messages[1].content[0].text).toContain("focus on decisions");
    expect(options).toEqual(expect.objectContaining({
      maxTokens: 800,
      reasoning: "high",
      signal,
      toolChoice: "none",
    }));

    expect(result).toEqual({
      summary: [
        "checkpoint summary",
        "",
        "<read-files>",
        "/tmp/read.md",
        "</read-files>",
        "",
        "<modified-files>",
        "/tmp/edited.md",
        "/tmp/written.md",
        "</modified-files>",
      ].join("\n"),
      firstKeptEntryId: "entry-keep",
      tokensBefore: 1234,
      details: {
        readFiles: ["/tmp/read.md"],
        modifiedFiles: ["/tmp/edited.md", "/tmp/written.md"],
      },
    });
  });

  it("writes cache-preserving compaction results back into the session branch", async () => {
    const preparation = {
      firstKeptEntryId: "entry-keep",
      tokensBefore: 4321,
      settings: { reserveTokens: 2000 },
    };
    const branch = [{ type: "message", id: "entry-old" }, { type: "message", id: "entry-keep" }];
    const compactedMessages = [{ role: "user", content: "after compaction" }];
    prepareCompactionMock.mockReturnValue(preparation);

    const appendCompaction = vi.fn();
    const replaceMessages = vi.fn();
    const session = {
      model: { id: "model", reasoning: false, contextWindow: 128000 },
      settingsManager: {
        getCompactionSettings: vi.fn(() => ({ enabled: true, reserveTokens: 2000 })),
      },
      sessionManager: {
        getBranch: vi.fn(() => branch),
        appendCompaction,
        buildSessionContext: vi.fn(() => ({ messages: compactedMessages })),
      },
      agent: {
        state: {
          systemPrompt: "system prompt",
          messages: [{ role: "user", content: "before compaction" }],
          tools: [],
          thinkingLevel: "off",
        },
        transformContext: vi.fn(async (messages) => [
          ...messages,
          { role: "assistant", content: "latest streamed answer" },
        ]),
        streamFn: vi.fn(async () => ({
          result: vi.fn(async () => ({
            stopReason: "stop",
            content: [{ type: "text", text: "cache summary" }],
          })),
        })),
        convertToLlm: vi.fn(async (messages) => messages),
        replaceMessages,
      },
    };

    const result = await runCachePreservingCompactionForSession(session);

    expect(prepareCompactionMock).toHaveBeenCalledWith(branch, { enabled: true, reserveTokens: 2000 });
    expect(session.agent.transformContext).toHaveBeenCalledWith(
      [{ role: "user", content: "before compaction" }],
      undefined,
    );
    expect(appendCompaction).toHaveBeenCalledWith(
      "cache summary",
      "entry-keep",
      4321,
      { readFiles: [], modifiedFiles: [] },
      true,
    );
    expect(replaceMessages).toHaveBeenCalledWith(compactedMessages);
    expect(result.summary).toBe("cache summary");
  });

  it("hard truncates direct session compaction when the cache-preserving request cannot fit", async () => {
    const preparation = {
      firstKeptEntryId: "entry-keep",
      tokensBefore: 9000,
      settings: { reserveTokens: 2000, keepRecentTokens: 100 },
    };
    const branch = [
      { type: "message", id: "entry-old", message: { role: "user", content: "old " + "x".repeat(2000) } },
      { type: "message", id: "entry-keep", message: { role: "assistant", content: [{ type: "text", text: "keep" }] } },
    ];
    const compactedMessages = [{ role: "compactionSummary", summary: "truncated" }];
    prepareCompactionMock.mockReturnValue(preparation);

    const appendCompaction = vi.fn();
    const replaceMessages = vi.fn();
    const streamFn = vi.fn(async () => ({
      result: vi.fn(async () => ({
        stopReason: "stop",
        content: [{ type: "text", text: "should not run" }],
      })),
    }));
    const session = {
      model: { id: "tiny", reasoning: false, contextWindow: 1000 },
      settingsManager: {
        getCompactionSettings: vi.fn(() => ({ enabled: true, reserveTokens: 2000, keepRecentTokens: 100 })),
      },
      sessionManager: {
        getBranch: vi.fn(() => branch),
        appendCompaction,
        buildSessionContext: vi.fn(() => ({ messages: compactedMessages })),
      },
      agent: {
        state: {
          systemPrompt: "system " + "x".repeat(2000),
          messages: [{ role: "user", content: [{ type: "text", text: "x".repeat(6000) }] }],
          tools: [],
          thinkingLevel: "off",
        },
        streamFn,
        convertToLlm: vi.fn(async (messages) => messages),
        replaceMessages,
      },
    };

    const result = await runCachePreservingCompactionForSession(session);

    expect(streamFn).not.toHaveBeenCalled();
    expect(appendCompaction).toHaveBeenCalledWith(
      expect.stringContaining("早期对话历史已被硬截断"),
      "entry-keep",
      expect.any(Number),
      expect.objectContaining({ reason: "cache-preserving-compaction-hard-truncate" }),
      true,
    );
    expect(replaceMessages).toHaveBeenCalledWith(compactedMessages);
    expect(result.details.reason).toBe("cache-preserving-compaction-hard-truncate");
  });

  it("hard truncates direct session compaction when model context window is unknown", async () => {
    const preparation = {
      firstKeptEntryId: "entry-keep",
      tokensBefore: 9000,
      settings: { reserveTokens: 2000, keepRecentTokens: 100 },
    };
    const branch = [
      { type: "message", id: "entry-old", message: { role: "user", content: "old context" } },
      { type: "message", id: "entry-keep", message: { role: "assistant", content: "keep" } },
    ];
    prepareCompactionMock.mockReturnValue(preparation);

    const appendCompaction = vi.fn();
    const streamFn = vi.fn(async () => ({
      result: vi.fn(async () => ({
        stopReason: "stop",
        content: [{ type: "text", text: "should not run" }],
      })),
    }));
    const session = {
      model: { id: "missing-window", reasoning: false },
      settingsManager: {
        getCompactionSettings: vi.fn(() => ({ enabled: true, reserveTokens: 2000, keepRecentTokens: 100 })),
      },
      sessionManager: {
        getBranch: vi.fn(() => branch),
        appendCompaction,
        buildSessionContext: vi.fn(() => ({ messages: [{ role: "compactionSummary", summary: "truncated" }] })),
      },
      agent: {
        state: {
          systemPrompt: "system prompt",
          messages: [{ role: "user", content: "before compaction" }],
          tools: [],
          thinkingLevel: "off",
        },
        streamFn,
        convertToLlm: vi.fn(async (messages) => messages),
        replaceMessages: vi.fn(),
      },
    };

    const result = await runCachePreservingCompactionForSession(session);

    expect(streamFn).not.toHaveBeenCalled();
    expect(appendCompaction).toHaveBeenCalledWith(
      expect.stringContaining("早期对话历史已被硬截断"),
      "entry-keep",
      expect.any(Number),
      expect.objectContaining({ reason: "cache-preserving-compaction-hard-truncate" }),
      true,
    );
    expect(result.details.reason).toBe("cache-preserving-compaction-hard-truncate");
  });

  it("emits lifecycle events for direct model-switch compaction", async () => {
    const preparation = {
      firstKeptEntryId: "entry-keep",
      tokensBefore: 4321,
      settings: { reserveTokens: 2000 },
    };
    const branch = [{ type: "message", id: "entry-old" }, { type: "message", id: "entry-keep" }];
    const compactedMessages = [{ role: "user", content: "after compaction" }];
    prepareCompactionMock.mockReturnValue(preparation);

    const appendCompaction = vi.fn(() => "compaction-entry");
    const emit = vi.fn();
    const extensionEmit = vi.fn(async () => {});
    const session = {
      model: { id: "model", reasoning: false, contextWindow: 128000 },
      _emit: emit,
      extensionRunner: {
        hasHandlers: vi.fn((event) => event === "session_compact"),
        emit: extensionEmit,
      },
      settingsManager: {
        getCompactionSettings: vi.fn(() => ({ enabled: true, reserveTokens: 2000 })),
      },
      sessionManager: {
        getBranch: vi.fn(() => branch),
        appendCompaction,
        getEntry: vi.fn(() => ({ type: "compaction", id: "compaction-entry", summary: "cache summary" })),
        buildSessionContext: vi.fn(() => ({ messages: compactedMessages })),
      },
      agent: {
        state: {
          systemPrompt: "system prompt",
          messages: [{ role: "user", content: "before compaction" }],
          tools: [],
          thinkingLevel: "off",
        },
        streamFn: vi.fn(async () => ({
          result: vi.fn(async () => ({
            stopReason: "stop",
            content: [{ type: "text", text: "cache summary" }],
          })),
        })),
        convertToLlm: vi.fn(async (messages) => messages),
        replaceMessages: vi.fn(),
      },
    };

    await runCachePreservingCompactionForSession(session, {
      emitLifecycle: true,
      lifecycleReason: "model_switch",
    });

    expect(emit).toHaveBeenNthCalledWith(1, { type: "compaction_start", reason: "model_switch" });
    expect(extensionEmit).toHaveBeenCalledWith({
      type: "session_compact",
      compactionEntry: { type: "compaction", id: "compaction-entry", summary: "cache summary" },
      fromExtension: true,
    });
    expect(emit).toHaveBeenLastCalledWith({
      type: "compaction_end",
      reason: "model_switch",
      result: expect.objectContaining({ summary: "cache summary" }),
      aborted: false,
      willRetry: false,
    });
  });

  it("refuses the manual wrapper when the compaction hook is missing", async () => {
    const session = {
      compact: vi.fn(),
      extensionRunner: { hasHandlers: vi.fn(() => false) },
    };

    await expect(compactSessionWithCachePreservation(session)).rejects.toThrow(
      "Cache-preserving compaction extension is not installed",
    );
    expect(session.compact).not.toHaveBeenCalled();
  });

  it("reports stale extension runners before invoking manual compaction", async () => {
    const session = {
      compact: vi.fn(),
      extensionRunner: {
        assertActive: vi.fn(() => {
          throw new Error("This extension ctx is stale after session replacement or reload.");
        }),
        hasHandlers: vi.fn(() => true),
      },
    };

    await expect(compactSessionWithCachePreservation(session)).rejects.toThrow(
      "This extension ctx is stale after session replacement or reload",
    );
    expect(session.extensionRunner.hasHandlers).not.toHaveBeenCalled();
    expect(session.compact).not.toHaveBeenCalled();
  });

  it("keeps Pi lifecycle events by delegating manual compaction through session.compact", async () => {
    const session = {
      compact: vi.fn(async () => "ok"),
      extensionRunner: { hasHandlers: vi.fn(() => true) },
    };

    await expect(compactSessionWithCachePreservation(session, "extra focus")).resolves.toBe("ok");
    expect(session.compact).toHaveBeenCalledWith("extra focus");
  });
});
