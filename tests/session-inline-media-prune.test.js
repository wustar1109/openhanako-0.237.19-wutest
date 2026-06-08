import { describe, expect, it, vi } from "vitest";
import { pruneSessionInlineMediaHistory } from "../core/session-inline-media-prune.js";

const IMG_BLOCK = { type: "image", data: "BASE64DATA", mimeType: "image/png" };
const TEXT_BLOCK = (text) => ({ type: "text", text });

describe("pruneSessionInlineMediaHistory", () => {
  it("从 session JSONL entries 和 agent runtime state 中移除 inline image base64", () => {
    const rewriteFile = vi.fn();
    const manager = {
      fileEntries: [
        { type: "session", id: "session" },
        {
          type: "message",
          id: "u1",
          message: {
            role: "user",
            content: [TEXT_BLOCK("[attached_image: /tmp/a.png]\n看图"), { ...IMG_BLOCK }],
          },
        },
        { type: "message", id: "a1", message: { role: "assistant", content: [TEXT_BLOCK("seen")] } },
      ],
      _rewriteFile: rewriteFile,
    };
    const session = {
      sessionManager: manager,
      agent: {
        state: {
          messages: [
            {
              role: "user",
              content: [TEXT_BLOCK("[attached_image: /tmp/a.png]\n看图"), { ...IMG_BLOCK }],
            },
            { role: "assistant", content: [TEXT_BLOCK("seen")] },
          ],
        },
      },
    };

    const result = pruneSessionInlineMediaHistory(session);

    expect(result.strippedImages).toBe(2);
    expect(manager.fileEntries[1].message.content).toEqual([
      TEXT_BLOCK("[attached_image: /tmp/a.png]\n看图"),
    ]);
    expect(session.agent.state.messages[0].content).toEqual(manager.fileEntries[1].message.content);
    expect(rewriteFile).toHaveBeenCalledTimes(1);
  });

  it("没有 inline media 时不重写 session 文件", () => {
    const rewriteFile = vi.fn();
    const session = {
      sessionManager: {
        fileEntries: [
          { type: "session", id: "session" },
          { type: "message", id: "u1", message: { role: "user", content: [TEXT_BLOCK("hi")] } },
        ],
        _rewriteFile: rewriteFile,
      },
      agent: { state: { messages: [{ role: "user", content: [TEXT_BLOCK("hi")] }] } },
    };

    const result = pruneSessionInlineMediaHistory(session);

    expect(result.stripped).toBe(0);
    expect(rewriteFile).not.toHaveBeenCalled();
  });
});
