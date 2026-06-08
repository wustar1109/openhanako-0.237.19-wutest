import {
  completeSimple,
  convertAgentMessagesToLlm,
  estimateTokens,
  prepareCompaction,
} from "../lib/pi-sdk/index.js";
import { computeHardTruncation } from "./compaction-utils.js";

const DEFAULT_HARD_TRUNCATE_THRESHOLD = 0.85;
const COMPACTION_REQUEST_BUFFER_TOKENS = 1024;

const COMPACTION_REQUEST_PREFIX = `[Hana cache-preserving compaction]

You are performing an internal context compaction for this same assistant session.
The full conversation prefix above is the source of truth. Do not answer the user
or continue the task. Produce only a structured checkpoint summary that a future
turn can use after older history is replaced by this summary.

Use this exact format:

## Goal
[What the user is trying to accomplish.]

## Constraints & Preferences
- [Important user constraints, project rules, tone preferences, or "(none)".]

## Progress
### Done
- [x] [Completed work]

### In Progress
- [ ] [Current unfinished work]

### Blocked
- [Blockers, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Concrete next step]

## Critical Context
- [Exact file paths, commands, errors, identifiers, dates, issue links, or facts needed to continue.]

Keep it concise, but preserve technical facts exactly. If recent messages will be
kept by the compactor, summarize them only when they clarify the older context.`;

function textBlock(text) {
  return { type: "text", text };
}

function estimateTextTokens(text) {
  if (typeof text !== "string" || text.length === 0) return 0;
  return Math.ceil(text.length / 4);
}

export function getCachePreservingCompactionMaxTokens(preparation) {
  return Math.max(512, Math.floor((preparation?.settings?.reserveTokens ?? 4096) * 0.8));
}

export function buildCachePreservingCompactionInstruction({ preparation, customInstructions } = {}) {
  const retainedNote = preparation?.firstKeptEntryId
    ? `\n\nThe compactor will keep the recent tail starting at session entry id ${preparation.firstKeptEntryId}. Focus the summary on context that may be removed, while preserving current intent and decisions.`
    : "";
  const splitTurnNote = preparation?.isSplitTurn
    ? "\n\nThe cut point is inside a turn. Include enough turn-prefix context for the retained suffix to make sense."
    : "";
  const customNote = customInstructions
    ? `\n\nAdditional focus from the caller: ${customInstructions}`
    : "";

  return {
    role: "user",
    content: [textBlock(`${COMPACTION_REQUEST_PREFIX}${retainedNote}${splitTurnNote}${customNote}`)],
    timestamp: Date.now(),
  };
}

function computeFileDetails(fileOps) {
  const read = fileOps?.read instanceof Set ? fileOps.read : new Set(fileOps?.read || []);
  const written = fileOps?.written instanceof Set ? fileOps.written : new Set(fileOps?.written || []);
  const edited = fileOps?.edited instanceof Set ? fileOps.edited : new Set(fileOps?.edited || []);
  const modified = new Set([...edited, ...written]);
  return {
    readFiles: [...read].filter((file) => !modified.has(file)).sort(),
    modifiedFiles: [...modified].sort(),
  };
}

function appendFileOperationContext(summary, details) {
  const sections = [];
  if (details.readFiles.length > 0) {
    sections.push(`<read-files>\n${details.readFiles.join("\n")}\n</read-files>`);
  }
  if (details.modifiedFiles.length > 0) {
    sections.push(`<modified-files>\n${details.modifiedFiles.join("\n")}\n</modified-files>`);
  }
  if (sections.length === 0) return summary;
  return `${summary.trimEnd()}\n\n${sections.join("\n\n")}`;
}

function extractSummaryText(response) {
  return response?.content
    ?.filter((block) => block?.type === "text" && typeof block.text === "string")
    ?.map((block) => block.text)
    ?.join("\n")
    ?.trim();
}

function isErrorResponse(response) {
  return response?.stopReason === "error" || response?.stopReason === "aborted";
}

export function isStaleExtensionContextError(error) {
  const message = error instanceof Error ? error.message : String(error || "");
  return message.includes("This extension ctx is stale after session replacement or reload");
}

export function estimateCachePreservingCompactionRequest({
  preparation,
  systemPrompt = "",
  messages = [],
  customInstructions,
} = {}) {
  const instruction = buildCachePreservingCompactionInstruction({ preparation, customInstructions });
  const messageTokens = Array.isArray(messages)
    ? messages.reduce((sum, message) => sum + estimateTokens(message), 0)
    : 0;
  const instructionTokens = estimateTokens(instruction);
  const systemPromptTokens = estimateTextTokens(systemPrompt);
  const maxTokens = getCachePreservingCompactionMaxTokens(preparation);
  const promptTokens = messageTokens + instructionTokens + systemPromptTokens + COMPACTION_REQUEST_BUFFER_TOKENS;
  return {
    promptTokens,
    maxTokens,
    totalTokens: promptTokens + maxTokens,
    messageTokens,
    instructionTokens,
    systemPromptTokens,
    bufferTokens: COMPACTION_REQUEST_BUFFER_TOKENS,
  };
}

export function shouldHardTruncateCachePreservingCompaction({
  preparation,
  model,
  systemPrompt,
  messages,
  customInstructions,
  hardTruncateThreshold = DEFAULT_HARD_TRUNCATE_THRESHOLD,
} = {}) {
  const contextWindow = model?.contextWindow ?? 0;
  const budget = estimateCachePreservingCompactionRequest({
    preparation,
    systemPrompt,
    messages,
    customInstructions,
  });
  if (contextWindow <= 0) {
    return { shouldHardTruncate: true, budget, threshold: 0, contextWindow };
  }
  const threshold = Math.floor(contextWindow * hardTruncateThreshold);
  return {
    shouldHardTruncate: budget.totalTokens > threshold,
    budget,
    threshold,
    contextWindow,
  };
}

function hardTruncateCachePreservingCompaction(branchEntries, preparation, {
  reason = "cache-preserving-compaction-hard-truncate",
  summary = "[由于对话过长且压缩请求本身会超限，早期对话历史已被硬截断（hana-cache-preserving-compaction）]",
} = {}) {
  const keepRecentTokens = preparation?.settings?.keepRecentTokens ?? 20_000;
  return computeHardTruncation(branchEntries, keepRecentTokens, {
    summary,
    reason,
  });
}

function emitCompactionProgress(session, event) {
  session?._emit?.(event);
}

async function emitSessionCompactEvent(session, compactionEntryId, fromExtension) {
  const runner = session?.extensionRunner;
  if (!runner?.hasHandlers?.("session_compact")) return;
  const compactionEntry = session.sessionManager?.getEntry?.(compactionEntryId)
    || session.sessionManager?.getEntries?.()?.find((entry) => entry?.id === compactionEntryId);
  if (!compactionEntry) return;
  await runner.emit({
    type: "session_compact",
    compactionEntry,
    fromExtension,
  });
}

export async function appendCompactionResultToSession(session, result, { fromExtension = true } = {}) {
  const compactionEntryId = session.sessionManager.appendCompaction(
    result.summary,
    result.firstKeptEntryId,
    result.tokensBefore,
    result.details,
    fromExtension,
  );
  replaceSessionMessages(session);
  await emitSessionCompactEvent(session, compactionEntryId, fromExtension);
  return result;
}

export async function createCachePreservingCompactionResult({
  preparation,
  model,
  systemPrompt,
  messages,
  customInstructions,
  signal,
  thinkingLevel,
  streamFn,
  streamOptions = {},
  convertToLlm = convertAgentMessagesToLlm,
}) {
  if (!preparation) throw new Error("Cache-preserving compaction requires preparation");
  if (!model) throw new Error("Cache-preserving compaction requires a model");
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new Error("Cache-preserving compaction requires conversation messages");
  }

  const instruction = buildCachePreservingCompactionInstruction({ preparation, customInstructions });
  const agentMessages = [...messages, instruction];
  const llmMessages = await convertToLlm(agentMessages);
  const maxTokens = getCachePreservingCompactionMaxTokens(preparation);
  const options = {
    ...streamOptions,
    maxTokens,
    signal,
    toolChoice: "none",
    ...(model.reasoning && thinkingLevel && thinkingLevel !== "off" ? { reasoning: thinkingLevel } : {}),
  };

  const context = {
    systemPrompt,
    messages: llmMessages,
  };
  const response = streamFn
    ? await (await streamFn(model, context, options)).result()
    : await completeSimple(model, context, options);

  if (isErrorResponse(response)) {
    throw new Error(`Cache-preserving compaction failed: ${response.errorMessage || response.stopReason || "unknown error"}`);
  }

  const text = extractSummaryText(response);
  if (!text) {
    throw new Error("Cache-preserving compaction failed: empty summary");
  }

  const details = computeFileDetails(preparation.fileOps);
  return {
    summary: appendFileOperationContext(text, details),
    firstKeptEntryId: preparation.firstKeptEntryId,
    tokensBefore: preparation.tokensBefore,
    details,
  };
}

function replaceSessionMessages(session) {
  const context = session.sessionManager.buildSessionContext();
  if (session.agent?.replaceMessages) {
    session.agent.replaceMessages(context.messages);
  } else if (session.agent?.state) {
    session.agent.state.messages = context.messages;
  }
}

export async function runCachePreservingCompactionForSession(session, {
  settings,
  model = session?.model,
  customInstructions,
  signal,
  hardTruncateThreshold = DEFAULT_HARD_TRUNCATE_THRESHOLD,
  emitLifecycle = false,
  lifecycleReason = "manual",
} = {}) {
  if (!session?.sessionManager) throw new Error("runCachePreservingCompactionForSession: missing session manager");
  if (!session?.agent) throw new Error("runCachePreservingCompactionForSession: missing agent");
  if (!model) throw new Error("runCachePreservingCompactionForSession: missing model");

  const compactionSettings = settings || session.settingsManager?.getCompactionSettings?.();
  if (!compactionSettings) throw new Error("runCachePreservingCompactionForSession: missing compaction settings");

  const branchEntries = session.sessionManager.getBranch();
  if (emitLifecycle) {
    emitCompactionProgress(session, { type: "compaction_start", reason: lifecycleReason });
  }

  try {
    const preparation = prepareCompaction(branchEntries, compactionSettings);
    if (!preparation) {
      const lastEntry = branchEntries[branchEntries.length - 1];
      if (lastEntry?.type === "compaction") throw new Error("Already compacted");
      throw new Error("Nothing to compact (session too small)");
    }

    let messages = session.agent.state?.messages?.length
      ? session.agent.state.messages
      : session.sessionManager.buildSessionContext().messages;
    if (session.agent.transformContext) {
      messages = await session.agent.transformContext(messages, signal);
    }

    const systemPrompt = session.agent.state?.systemPrompt ?? session.systemPrompt;
    const fit = shouldHardTruncateCachePreservingCompaction({
      preparation,
      model,
      systemPrompt,
      messages,
      customInstructions,
      hardTruncateThreshold,
    });
    if (fit.shouldHardTruncate) {
      const truncation = hardTruncateCachePreservingCompaction(branchEntries, preparation);
      if (!truncation) {
        throw new Error(
          `Cache-preserving compaction request exceeds model window ` +
          `(${fit.budget.totalTokens} > ${fit.threshold}) and hard truncation is unavailable`
        );
      }
      const result = await appendCompactionResultToSession(session, truncation, { fromExtension: true });
      if (emitLifecycle) {
        emitCompactionProgress(session, {
          type: "compaction_end",
          reason: lifecycleReason,
          result,
          aborted: false,
          willRetry: false,
        });
      }
      return result;
    }

    const result = await createCachePreservingCompactionResult({
      preparation,
      model,
      systemPrompt,
      messages,
      customInstructions,
      signal,
      thinkingLevel: session.thinkingLevel ?? session.agent.state?.thinkingLevel,
      streamFn: session.agent.streamFn,
      streamOptions: {
        sessionId: session.agent.sessionId,
        onPayload: session.agent.onPayload,
        onResponse: session.agent.onResponse,
        transport: session.agent.transport,
        thinkingBudgets: session.agent.thinkingBudgets,
        maxRetryDelayMs: session.agent.maxRetryDelayMs,
      },
      convertToLlm: session.agent.convertToLlm,
    });

    const saved = await appendCompactionResultToSession(session, result, { fromExtension: true });
    if (emitLifecycle) {
      emitCompactionProgress(session, {
        type: "compaction_end",
        reason: lifecycleReason,
        result: saved,
        aborted: false,
        willRetry: false,
      });
    }
    return saved;
  } catch (error) {
    if (emitLifecycle) {
      const message = error instanceof Error ? error.message : String(error);
      const aborted = signal?.aborted || message === "Compaction cancelled" || error?.name === "AbortError";
      emitCompactionProgress(session, {
        type: "compaction_end",
        reason: lifecycleReason,
        result: undefined,
        aborted,
        willRetry: false,
        errorMessage: aborted ? undefined : `Compaction failed: ${message}`,
      });
    }
    throw error;
  }
}

export async function compactSessionWithCachePreservation(session, customInstructions) {
  session?.extensionRunner?.assertActive?.();
  if (!session?.extensionRunner?.hasHandlers?.("session_before_compact")) {
    throw new Error("Cache-preserving compaction extension is not installed for this session");
  }
  return await session.compact(customInstructions);
}
