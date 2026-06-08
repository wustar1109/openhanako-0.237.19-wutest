import { describe, expect, it } from "vitest";
import {
  modelSupportsAnthropicMaxEffort,
  modelSupportsXhigh,
  normalizeThinkingLevelForModel,
} from "../core/session-thinking-level.js";

describe("session thinking level capabilities", () => {
  it("shows the unified Max level for GPT-5.5", () => {
    const model = { id: "gpt-5.5", provider: "openai", reasoning: true };

    expect(modelSupportsXhigh(model)).toBe(true);
    expect(normalizeThinkingLevelForModel("xhigh", model)).toBe("xhigh");
  });

  it("shows the unified Max level for Claude models with Anthropic max effort", () => {
    const models = [
      { id: "claude-opus-4-7", provider: "anthropic", reasoning: true },
      { id: "claude-opus-4-6", provider: "anthropic", reasoning: true },
      { id: "claude-sonnet-4-6", provider: "anthropic", reasoning: true },
      { id: "anthropic/claude-opus-4-7", provider: "vercel-ai-gateway", api: "anthropic-messages", reasoning: true },
    ];

    for (const model of models) {
      expect(modelSupportsXhigh(model)).toBe(true);
      expect(modelSupportsAnthropicMaxEffort(model)).toBe(true);
      expect(normalizeThinkingLevelForModel("xhigh", model)).toBe("xhigh");
    }
  });

  it("does not infer Anthropic max effort for non-Anthropic wire formats by model name alone", () => {
    const model = {
      id: "anthropic/claude-opus-4-7",
      provider: "openrouter",
      api: "openai-completions",
      reasoning: true,
    };

    expect(modelSupportsAnthropicMaxEffort(model)).toBe(false);
  });
});
