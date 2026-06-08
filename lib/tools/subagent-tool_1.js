/**
 * subagent-tool.js — Sub-agent 工具（非阻塞）
 *
 * 将独立子任务派给隔离的 agent session 执行，支持通过 agent 参数指定目标 agent。
 * 任务在后台运行，完成后通过 DeferredResultStore 持久化结果，
 * deferred-result-ext 以 steer 消息注入对话。
 * 调用方无需等待，可继续与用户对话。
 *
 * agent="?" 时列出所有可用 agent（同步返回）。
 */

import { Type } from "../pi-sdk/index.js";
import path from "node:path";
import { t } from "../../server/i18n.js";
import { getToolSessionCwd, getToolSessionPath } from "./tool-session.js";
import { resolveAgentParam } from "./agent-id-resolver.js";
import {
  mergeExecutorMetadata,
  normalizeExecutorMetadata,
} from "../subagent-executor-metadata.js";

// Subagent 工具白名单：精简到"调研 + 工程"所需的最小集
// Custom：只留网页检索、本地网页抓取、todo 规划、浏览器（登录场景）
// Builtin：Pi SDK 内置的文件/shell 工具全部保留（read/write/edit/bash/grep/find/ls）
// 故意不给 subagent：所有记忆相关工具、cron/notify/stop_task/update_settings、channel/dm、
// subagent（防自递归）、install_skill、stage_files、旧 artifact 兼容层、check_deferred、wait
const SUBAGENT_CUSTOM_TOOLS = ["web_search", "web_fetch", "todo_write", "browser"];
const SUBAGENT_BUILTIN_TOOLS = ["read", "write", "edit", "bash", "grep", "find", "ls"];
const SUBAGENT_TIMEOUT_MS = 15 * 60 * 1000; // 15 分钟

// 并发限制在 createSubagentTool 闭包内（per-agent），不再全局共享

function extractTaskTitle(task) {
  return String(task || "")
    .split(/\r?\n/)
    .map(line => line.trim())
    .find(Boolean) || "";
}

function formatAgentEntry(a) {
  const label = a.name && a.name !== a.id ? `${a.id} (${a.name})` : a.id;
  const parts = [label];
  if (a.model) parts.push(`[${a.model}]`);
  if (a.summary) parts.push(a.summary);
  return parts.join(" — ");
}

function resolveAgentIdentity(listAgents, currentAgentId, agentId) {
  const actualAgentId = agentId || currentAgentId || null;
  if (!actualAgentId) {
    return normalizeExecutorMetadata({});
  }

  const agents = listAgents ? listAgents() : [];
  const target = agents.find(a => a.id === actualAgentId);
  return normalizeExecutorMetadata({
    agentId: actualAgentId,
    agentName: target?.name || target?.agentName || actualAgentId,
  });
}

function applyRequestedAgentMetadata(target, requestedIdentity) {
  if (!target || !requestedIdentity) return target;
  target.requestedAgentId = requestedIdentity.executorAgentId;
  target.requestedAgentNameSnapshot = requestedIdentity.executorAgentNameSnapshot;
  return target;
}

function collectSessionFiles(result) {
  const files = [];
  const push = (item) => {
    if (item && typeof item === "object") files.push(item);
  };
  if (Array.isArray(result?.sessionFiles)) {
    for (const item of result.sessionFiles) push(item);
  }
  if (Array.isArray(result?.files)) {
    for (const item of result.files) push(item);
  }
  return files;
}

function describeSessionFile(file) {
  const label = file?.label || file?.displayName || file?.filename || file?.name || null;
  const filePath = file?.filePath || file?.path || file?.realPath || null;
  if (label && filePath && label !== filePath) return `${label}: ${filePath}`;
  return filePath || label || null;
}

function formatProducedFiles(files) {
  const lines = files.map(describeSessionFile).filter(Boolean);
  if (!lines.length) return "";
  return t("error.subagentProducedFiles", {
    files: lines.map(line => `- ${line}`).join("\n"),
  });
}

function completionErrorForStopReason(stopReason, errorMessage) {
  if (!stopReason || stopReason === "stop") return null;
  if (stopReason === "error") {
    return errorMessage || t("error.subagentStopError");
  }
  if (stopReason === "length") {
    return t("error.subagentStopLength");
  }
  return t("error.subagentStopReason", { reason: stopReason });
}

function normalizeSubagentOutcome(result) {
  const stopError = completionErrorForStopReason(result?.stopReason, result?.errorMessage);
  if (result?.error) {
    return { ok: false, reason: stopError || String(result.error) };
  }
  if (stopError) return { ok: false, reason: stopError };
  const text = typeof result?.replyText === "string" && result.replyText.trim()
    ? result.replyText
    : "";
  const sessionFiles = collectSessionFiles(result);
  if (text) {
    return { ok: true, text, sessionFiles };
  }
  const fileSummary = formatProducedFiles(sessionFiles);
  if (fileSummary) {
    return { ok: true, text: fileSummary, sessionFiles };
  }
  if (Array.isArray(result?.toolErrors) && result.toolErrors.length) {
    return { ok: false, reason: t("error.subagentToolFailed", { msg: result.toolErrors.filter(Boolean).join("; ") }) };
  }
  return { ok: false, reason: t("error.subagentNoOutput") };
}

/**
 * @param {object} deps
 * @param {(opts: object) => Promise<{ sessionPath: string|null, run: (prompt: string) => Promise }>} deps.prepareIsolatedSession
 * @param {() => string|null} deps.resolveUtilityModel
 * @param {() => import("../deferred-result-store.js").DeferredResultStore|null} deps.getDeferredStore
 * @param {() => import("../subagent-run-store.js").SubagentRunStore|null} [deps.getSubagentRunStore]
 * @param {() => string|null} deps.getSessionPath
 * @param {() => string|null} [deps.getParentCwd] - parent session 当前工作目录，subagent 继承它
 * @param {() => Array} [deps.listAgents]
 * @param {string} [deps.currentAgentId]
 * @param {(event: object, sessionPath?: string|null) => void} [deps.emitEvent]
 */
export function createSubagentTool(deps) {
  const activeBySession = new Map(); // sessionPath → count
  const MAX_PER_SESSION = 8;
  const MAX_GLOBAL = 20;

  function getActive(sp) { return activeBySession.get(sp) || 0; }
  function incActive(sp) { activeBySession.set(sp, getActive(sp) + 1); }
  function decActive(sp) {
    const n = getActive(sp) - 1;
    if (n <= 0) activeBySession.delete(sp);
    else activeBySession.set(sp, n);
  }
  function totalActive() {
    let sum = 0;
    for (const v of activeBySession.values()) sum += v;
    return sum;
  }

  return {
    name: "subagent",
    label: t("toolDef.subagent.label"),
    description: t("toolDef.subagent.description"),
    parameters: Type.Object({
      task: Type.String({ description: t("toolDef.subagent.taskDesc") }),
      model: Type.Optional(Type.String({ description: t("toolDef.subagent.modelDesc") })),
      agent: Type.Optional(Type.String({ description: t("toolDef.subagent.agentDesc") })),
    }),

    execute: async (_toolCallId, params, _signal, _onUpdate, ctx) => {
      // discovery 模式
      if (params.agent === "?" || params.agent === "list") {
        const listAgents = deps.listAgents;
        if (!listAgents) {
          return { content: [{ type: "text", text: t("error.noOtherAgents") }] };
        }
        const agents = listAgents().filter(a => a.id !== deps.currentAgentId);
        if (!agents.length) {
          return { content: [{ type: "text", text: t("error.noOtherAgents") }] };
        }
        return { content: [{ type: "text", text: agents.map(a => "- " + formatAgentEntry(a)).join("\n") }] };
      }

      // 解析 agent 参数：先按 id 严格匹配，找不到再按 name 唯一匹配兜底（防御 LLM 把显示名当 id 用）
      const allAgents = deps.listAgents ? deps.listAgents() : [];
      const resolved = resolveAgentParam(allAgents, params.agent);
      if (!resolved.ok) {
        const candidates = resolved.ambiguous
          ? resolved.byName
          : allAgents.filter(a => a.id !== deps.currentAgentId);
        return {
          content: [{
            type: "text",
            text: t("error.agentNotFoundAvailable", {
              id: params.agent,
              ids: candidates.map(formatAgentEntry).join("\n") || "(none)",
            }),
          }],
        };
      }
      // self-check：解析后是自己，视为未指定
      const targetAgentId = (resolved.agentId && resolved.agentId !== deps.currentAgentId)
        ? resolved.agentId
        : undefined;
      const requestedIdentity = resolveAgentIdentity(deps.listAgents, deps.currentAgentId, targetAgentId);

      const parentSessionPath = getToolSessionPath(ctx);
      const parentCwd = getToolSessionCwd(ctx) || deps.getParentCwd?.() || null;

      // 检查并发限制：per-session + global
      if (parentSessionPath && getActive(parentSessionPath) >= MAX_PER_SESSION) {
        return {
          content: [{ type: "text", text: t("error.subagentMaxConcurrent", { max: MAX_PER_SESSION }) }],
        };
      }
      if (totalActive() >= MAX_GLOBAL) {
        return {
          content: [{ type: "text", text: t("error.subagentMaxConcurrent", { max: MAX_GLOBAL }) }],
        };
      }

      const store = deps.getDeferredStore?.();
      const runStore = deps.getSubagentRunStore?.();

      if (!store || !parentSessionPath) {
        // deferred 基础设施不可用时同步 fallback
        return _syncFallback(deps, params, targetAgentId, parentSessionPath, parentCwd, { inc: () => incActive(parentSessionPath), dec: () => decActive(parentSessionPath) });
      }

      const taskId = `subagent-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const taskTitle = extractTaskTitle(params.task);
      const taskSummary = taskTitle.length > 80
        ? taskTitle.slice(0, 80) + "…"
        : taskTitle;

      store.defer(
        taskId,
        parentSessionPath,
        applyRequestedAgentMetadata(
          mergeExecutorMetadata({
            type: "subagent",
            summary: taskSummary,
          }, requestedIdentity),
          requestedIdentity,
        ),
      );
      runStore?.register?.(taskId, {
        parentSessionPath,
        summary: taskSummary,
        requestedAgentId: requestedIdentity?.executorAgentId || null,
        requestedAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
        executorAgentId: requestedIdentity?.executorAgentId || null,
        executorAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
        executorMetaVersion: requestedIdentity?.executorMetaVersion || null,
      });

      const controller = new AbortController();
      const timeoutTimer = setTimeout(() => controller.abort(), SUBAGENT_TIMEOUT_MS);
      if (timeoutTimer.unref) timeoutTimer.unref();

      const registry = deps.getTaskRegistry?.();
      registry?.register(taskId, {
        type: "subagent",
        parentSessionPath,
        meta: applyRequestedAgentMetadata(
          mergeExecutorMetadata({ summary: taskSummary }, requestedIdentity),
          requestedIdentity,
        ),
      });
      deps.setSubagentController?.(taskId, controller);

      incActive(parentSessionPath);

      // 原子执行，fire-and-forget。sessionPath 通过 onSessionReady 回调后补到前端。
      const executeForAgent = (agentId) => {
        const executorIdentity = resolveAgentIdentity(deps.listAgents, deps.currentAgentId, agentId);
        // 快照 parent session cwd：subagent 在"派出那一刻" parent 所在的目录干活，
        // 即使 parent 之后切了 cwd 也不影响已派出的 subagent。
        const inheritedCwd = parentCwd || undefined;
        return deps.executeIsolated(
          params.task,
          {
            agentId,
            cwd: inheritedCwd,
            parentSessionPath,
            emitEvents: true,
            persist: path.join(deps.agentDir, "subagent-sessions"),
            model: params.model,
            toolFilter: SUBAGENT_CUSTOM_TOOLS,
            builtinFilter: SUBAGENT_BUILTIN_TOOLS,
            subagentContext: true,
            fileReadSessionPaths: parentSessionPath ? [parentSessionPath] : [],
            signal: controller.signal,
            onSessionReady: (sp) => {
              // session 创建后立即后补 streamKey + 实际执行者身份
              deps.emitEvent?.({
                type: "block_update", taskId,
                patch: {
                  streamKey: sp,
                  agentId: executorIdentity?.executorAgentId || null,
                  agentName: executorIdentity?.executorAgentNameSnapshot || null,
                  executorAgentId: executorIdentity?.executorAgentId || null,
                  executorAgentNameSnapshot: executorIdentity?.executorAgentNameSnapshot || null,
                  requestedAgentId: requestedIdentity?.executorAgentId || null,
                  requestedAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
                },
              }, parentSessionPath);
              // 持久化子代理 sessionPath + 实际执行者身份到 deferred store meta（历史加载用）
              const task = store.query(taskId);
              if (task?.meta) {
                task.meta.sessionPath = sp;
                mergeExecutorMetadata(task.meta, executorIdentity);
                applyRequestedAgentMetadata(task.meta, requestedIdentity);
              }
              store._save?.();
              runStore?.attachSession?.(taskId, sp, {
                requestedAgentId: requestedIdentity?.executorAgentId || null,
                requestedAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
                executorAgentId: executorIdentity?.executorAgentId || null,
                executorAgentNameSnapshot: executorIdentity?.executorAgentNameSnapshot || null,
                executorMetaVersion: executorIdentity?.executorMetaVersion || null,
              });
              void deps.persistSubagentSessionMeta?.(sp, executorIdentity)?.catch?.(() => {});
            },
          },
        );
      };

      executeForAgent(targetAgentId).then(result => {
        const wasUserAborted = registry?.query(taskId)?.aborted;
        if (wasUserAborted) {
          store.abort(taskId, t("error.subagentAborted"));
          runStore?.abort?.(taskId, t("error.subagentAborted"));
          deps.emitEvent?.({
            type: "block_update", taskId,
            patch: { streamStatus: "aborted", summary: t("error.subagentAborted") },
          }, parentSessionPath);
          return;
        }
        const outcome = normalizeSubagentOutcome(result);
        if (!outcome.ok) {
          store.fail(taskId, outcome.reason);
          runStore?.fail?.(taskId, outcome.reason);
        } else {
          store.resolve(taskId, outcome.text);
          runStore?.resolve?.(taskId, outcome.text);
        }
        const summary = outcome.ok ? outcome.text : outcome.reason;
        deps.emitEvent?.({
          type: "block_update", taskId,
          patch: {
            streamStatus: outcome.ok ? "done" : "failed",
            summary: (summary || "").slice(0, 200),
          },
        }, parentSessionPath);
      }).catch(err => {
        const wasUserAborted = registry?.query(taskId)?.aborted;
        const isTimeout = err.name === "AbortError" || err.name === "TimeoutError";
        const reason = wasUserAborted
          ? t("error.subagentAborted")
          : isTimeout
            ? t("error.subagentTimeout", { minutes: SUBAGENT_TIMEOUT_MS / 60000 })
            : err.message || String(err);

        if (wasUserAborted) {
          store.abort(taskId, reason);
          runStore?.abort?.(taskId, reason);
        } else {
          store.fail(taskId, reason);
          runStore?.fail?.(taskId, reason);
        }

        deps.emitEvent?.({
          type: "block_update", taskId,
          patch: { streamStatus: wasUserAborted ? "aborted" : "failed", summary: reason },
        }, parentSessionPath);
      }).finally(() => {
        clearTimeout(timeoutTimer);
        deps.removeSubagentController?.(taskId);
        registry?.remove(taskId);
        decActive(parentSessionPath);
      });

      return {
        content: [{ type: "text", text: t("error.subagentDispatched", { taskId }) }],
        details: {
          taskId,
          task: params.task,
          taskTitle,
          agentId: requestedIdentity?.executorAgentId || null,
          agentName: requestedIdentity?.executorAgentNameSnapshot || null,
          requestedAgentId: requestedIdentity?.executorAgentId || null,
          requestedAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
          executorAgentId: requestedIdentity?.executorAgentId || null,
          executorAgentNameSnapshot: requestedIdentity?.executorAgentNameSnapshot || null,
          executorMetaVersion: requestedIdentity?.executorMetaVersion || null,
          sessionPath: null,  // 通过 block_update 后补 streamKey
          streamStatus: "running",
        },
      };
    },
  };
}

/** deferred 不可用时的同步 fallback */
async function _syncFallback(deps, params, targetAgentId, _sessionPath, parentCwd, counter) {
  const timeoutSignal = AbortSignal.timeout(SUBAGENT_TIMEOUT_MS);
  counter.inc();
  try {
    const result = await deps.executeIsolated(
      params.task,
      {
        agentId: targetAgentId,
        cwd: parentCwd || undefined,
        parentSessionPath: _sessionPath || null,
        model: params.model,
        toolFilter: SUBAGENT_CUSTOM_TOOLS,
        builtinFilter: SUBAGENT_BUILTIN_TOOLS,
        subagentContext: true,
        fileReadSessionPaths: _sessionPath ? [_sessionPath] : [],
        signal: timeoutSignal,
      },
    );
    const outcome = normalizeSubagentOutcome(result);
    if (!outcome.ok) {
      return { content: [{ type: "text", text: t("error.subagentFailed", { msg: outcome.reason }) }] };
    }
    return { content: [{ type: "text", text: outcome.text }] };
  } catch (err) {
    return { content: [{ type: "text", text: t("error.subagentFailed", { msg: err.message }) }] };
  } finally {
    counter.dec();
  }
}
