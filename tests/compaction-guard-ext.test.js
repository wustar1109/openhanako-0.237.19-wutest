import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock compaction-utils 以便精准控制 L3 判断和硬截断结果
vi.mock("../core/compaction-utils.js", () => ({
  computeHardTruncation: vi.fn(),
  estimatePreparationTokens: vi.fn(),
  truncateTextHeadTail: vi.fn(),
}));

import { createCompactionGuardExtension } from "../lib/extensions/compaction-guard-ext.js";
import {
  computeHardTruncation,
  estimatePreparationTokens,
  truncateTextHeadTail,
} from "../core/compaction-utils.js";

function createMockPi() {
  const handlers = {};
  return {
    on: vi.fn((event, handler) => {
      handlers[event] = handler;
    }),
    getThinkingLevel: vi.fn(() => "off"),
    getActiveTools: vi.fn(() => ["read"]),
    getAllTools: vi.fn(() => [{
      name: "read",
      description: "Read files",
      parameters: { type: "object", properties: {} },
    }]),
    trigger(event, ...args) {
      return handlers[event]?.(...args);
    },
    getHandler(event) {
      return handlers[event];
    },
  };
}

describe("CompactionGuardExtension", () => {
  let pi;
  let cacheCompactor;

  beforeEach(() => {
    vi.clearAllMocks();
    pi = createMockPi();
    cacheCompactor = vi.fn(async ({ preparation }) => ({
      summary: "cache summary",
      firstKeptEntryId: preparation.firstKeptEntryId || "uuid-42",
      tokensBefore: preparation.tokensBefore ?? 90_000,
      details: { readFiles: [], modifiedFiles: [] },
    }));
    createCompactionGuardExtension({ cacheCompactor })(pi);
  });

  it("registers context, message_end, tool_result and session_before_compact handlers", () => {
    expect(pi.on).toHaveBeenCalledWith("context", expect.any(Function));
    expect(pi.on).toHaveBeenCalledWith("message_end", expect.any(Function));
    expect(pi.on).toHaveBeenCalledWith("tool_result", expect.any(Function));
    expect(pi.on).toHaveBeenCalledWith("session_before_compact", expect.any(Function));
  });

  describe("L1: tool_result truncation", () => {
    it("leaves short text unchanged", async () => {
      truncateTextHeadTail.mockReturnValue({ text: "short", truncated: false, originalBytes: 5 });
      const res = await pi.trigger("tool_result", {
        toolName: "read",
        isError: false,
        content: [{ type: "text", text: "short" }],
      });
      expect(res).toBeUndefined();
    });

    it("replaces long text content with truncated version", async () => {
      truncateTextHeadTail.mockReturnValue({
        text: "HEAD...[省略]...TAIL",
        truncated: true,
        originalBytes: 200_000,
      });
      const res = await pi.trigger("tool_result", {
        toolName: "read",
        isError: false,
        content: [{ type: "text", text: "x".repeat(200_000) }],
      });
      expect(res).toEqual({ content: [{ type: "text", text: "HEAD...[省略]...TAIL" }] });
    });

    it("does NOT truncate error results (preserves diagnostic info)", async () => {
      const res = await pi.trigger("tool_result", {
        toolName: "bash",
        isError: true,
        content: [{ type: "text", text: "x".repeat(100_000) }],
      });
      expect(res).toBeUndefined();
      expect(truncateTextHeadTail).not.toHaveBeenCalled();
    });

    it("does NOT touch image blocks", async () => {
      truncateTextHeadTail.mockReturnValue({ text: "", truncated: false, originalBytes: 0 });
      const res = await pi.trigger("tool_result", {
        toolName: "read",
        isError: false,
        content: [{ type: "image", source: { data: "..." } }],
      });
      expect(res).toBeUndefined();
      expect(truncateTextHeadTail).not.toHaveBeenCalled();
    });

    it("mixes truncated text blocks with untouched image blocks", async () => {
      truncateTextHeadTail.mockReturnValueOnce({
        text: "TRUNCATED",
        truncated: true,
        originalBytes: 100_000,
      });
      const res = await pi.trigger("tool_result", {
        toolName: "read",
        isError: false,
        content: [
          { type: "text", text: "x".repeat(100_000) },
          { type: "image", source: { data: "..." } },
        ],
      });
      expect(res).toEqual({
        content: [
          { type: "text", text: "TRUNCATED" },
          { type: "image", source: { data: "..." } },
        ],
      });
    });

    it("swallows hook exceptions and returns undefined (passthrough)", async () => {
      truncateTextHeadTail.mockImplementation(() => {
        throw new Error("boom");
      });
      const res = await pi.trigger("tool_result", {
        toolName: "read",
        isError: false,
        content: [{ type: "text", text: "x".repeat(100_000) }],
      });
      expect(res).toBeUndefined();
    });

    it("returns undefined when content is not an array", async () => {
      const res = await pi.trigger("tool_result", { toolName: "custom", isError: false, content: null });
      expect(res).toBeUndefined();
    });
  });

  describe("L3: session_before_compact preemptive hard truncate", () => {
    const model = { id: "m", provider: "p", contextWindow: 128_000 };
    const preparation = {
      firstKeptEntryId: "uuid-42",
      messagesToSummarize: [{ role: "user", content: "..." }],
      tokensBefore: 90_000,
      settings: { keepRecentTokens: 20_000 },
    };
    const ctx = {
      model,
      modelRegistry: {
        getApiKeyAndHeaders: vi.fn(async () => ({ ok: true, apiKey: "key", headers: { "x-test": "1" } })),
      },
      getSystemPrompt: vi.fn(() => "system prompt"),
      sessionManager: {
        getBranch: () => [],
        buildSessionContext: () => ({
          thinkingLevel: "off",
          messages: [{ role: "user", content: [{ type: "text", text: "hello" }], timestamp: 1 }],
        }),
      },
    };

    it("returns cache-preserving compaction when summarize tokens are within threshold", async () => {
      estimatePreparationTokens.mockReturnValue(50_000); // < 128K * 0.85 = 108,800
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );
      expect(res).toEqual({
        compaction: {
          summary: "cache summary",
          firstKeptEntryId: "uuid-42",
          tokensBefore: 90_000,
          details: { readFiles: [], modifiedFiles: [] },
        },
      });
      expect(cacheCompactor).toHaveBeenCalledWith(expect.objectContaining({
        preparation,
        model,
        systemPrompt: "system prompt",
        customInstructions: undefined,
        thinkingLevel: "off",
      }));
      expect(cacheCompactor.mock.calls[0][0].tools).toBeUndefined();
      expect(computeHardTruncation).not.toHaveBeenCalled();
    });

    it("uses the latest transformed context plus final assistant message", async () => {
      estimatePreparationTokens.mockReturnValue(50_000);
      await pi.trigger("context", {
        messages: [{ role: "user", content: [{ type: "text", text: "hello" }], timestamp: 1 }],
      });
      await pi.trigger("message_end", {
        message: { role: "assistant", content: [{ type: "text", text: "done" }], timestamp: 2 },
      });

      await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );

      expect(cacheCompactor.mock.calls[0][0].messages).toHaveLength(2);
      expect(cacheCompactor.mock.calls[0][0].messages[1]).toMatchObject({ role: "assistant" });
    });

    it("does not read stale session-bound pi helpers during compaction", async () => {
      estimatePreparationTokens.mockReturnValue(50_000);
      pi.getThinkingLevel.mockImplementation(() => {
        throw new Error("This extension ctx is stale after session replacement or reload.");
      });

      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );

      expect(res).toMatchObject({ compaction: expect.any(Object) });
      expect(pi.getThinkingLevel).not.toHaveBeenCalled();
      expect(cacheCompactor).toHaveBeenCalledWith(expect.objectContaining({
        thinkingLevel: "off",
      }));
    });

    it("returns hard truncation when the full cache-preserving request would exceed the budget", async () => {
      estimatePreparationTokens.mockReturnValue(100); // old Pi summarizer estimate fits
      computeHardTruncation.mockReturnValue({
        summary: "[hard truncated for full request]",
        firstKeptEntryId: "uuid-42",
        tokensBefore: 90_000,
        details: { reason: "compaction-guard-hard-truncate" },
      });
      const branch = [{ type: "message", id: "a" }, { type: "message", id: "b" }];
      const tinyModel = { ...model, contextWindow: 1000 };
      const res = await pi.trigger(
        "session_before_compact",
        { preparation: { ...preparation, settings: { keepRecentTokens: 100, reserveTokens: 512 } }, signal: { aborted: false } },
        {
          ...ctx,
          model: tinyModel,
          getSystemPrompt: vi.fn(() => "system " + "x".repeat(1000)),
          sessionManager: {
            ...ctx.sessionManager,
            getBranch: () => branch,
            buildSessionContext: () => ({
              thinkingLevel: "off",
              messages: [{ role: "user", content: [{ type: "text", text: "x".repeat(6000) }], timestamp: 1 }],
            }),
          },
        },
      );

      expect(res).toEqual({
        compaction: {
          summary: "[hard truncated for full request]",
          firstKeptEntryId: "uuid-42",
          tokensBefore: 90_000,
          details: { reason: "compaction-guard-hard-truncate" },
        },
      });
      expect(computeHardTruncation).toHaveBeenCalledWith(branch, 100, expect.objectContaining({
        reason: "compaction-guard-hard-truncate",
      }));
      expect(cacheCompactor).not.toHaveBeenCalled();
    });

    it("returns hard truncation when summarize tokens exceed threshold", async () => {
      estimatePreparationTokens.mockReturnValue(120_000); // > 108,800
      computeHardTruncation.mockReturnValue({
        summary: "[hard truncated]",
        firstKeptEntryId: "uuid-42",
        tokensBefore: 90_000,
        details: { reason: "compaction-guard-hard-truncate" },
      });
      const branch = [{ type: "message", id: "a" }, { type: "message", id: "b" }];
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        { ...ctx, sessionManager: { ...ctx.sessionManager, getBranch: () => branch } },
      );
      expect(res).toEqual({
        compaction: {
          summary: "[hard truncated]",
          firstKeptEntryId: "uuid-42",
          tokensBefore: 90_000,
          details: { reason: "compaction-guard-hard-truncate" },
        },
      });
      expect(computeHardTruncation).toHaveBeenCalledWith(branch, 20_000, expect.objectContaining({
        reason: "compaction-guard-hard-truncate",
      }));
      expect(cacheCompactor).not.toHaveBeenCalled();
    });

    it("cancels when hard truncate itself fails", async () => {
      estimatePreparationTokens.mockReturnValue(120_000);
      computeHardTruncation.mockReturnValue(null); // 无法截断
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );
      expect(res).toEqual({ cancel: true });
    });

    it("cancels when signal already aborted", async () => {
      estimatePreparationTokens.mockReturnValue(120_000);
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: true } },
        ctx,
      );
      expect(res).toEqual({ cancel: true });
      expect(computeHardTruncation).not.toHaveBeenCalled();
    });

    it("cancels when model is missing", async () => {
      estimatePreparationTokens.mockReturnValue(120_000);
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        { ...ctx, model: undefined },
      );
      expect(res).toEqual({ cancel: true });
    });

    it("cancels when contextWindow is 0", async () => {
      estimatePreparationTokens.mockReturnValue(120_000);
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        { ...ctx, model: { ...model, contextWindow: 0 } },
      );
      expect(res).toEqual({ cancel: true });
    });

    it("swallows hook exceptions and cancels", async () => {
      estimatePreparationTokens.mockImplementation(() => {
        throw new Error("boom");
      });
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );
      expect(res).toEqual({ cancel: true });
    });

    it("honors custom hardTruncateThreshold option", async () => {
      pi = createMockPi();
      createCompactionGuardExtension({ hardTruncateThreshold: 0.5, cacheCompactor })(pi);
      // 50% * 128K = 64K
      estimatePreparationTokens.mockReturnValue(70_000); // > 64K 应触发
      computeHardTruncation.mockReturnValue({
        summary: "s", firstKeptEntryId: "id", tokensBefore: 0, details: {},
      });
      const res = await pi.trigger(
        "session_before_compact",
        { preparation, signal: { aborted: false } },
        ctx,
      );
      expect(res).toMatchObject({ compaction: expect.any(Object) });
    });
  });
});
