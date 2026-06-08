/**
 * plugins/image-gen/tools/generate-image.js
 *
 * Non-blocking image generation. Registers a local task immediately, then
 * submits to the provider in the background. Completion is delivered through
 * Poller + DeferredResultStore.
 */
import path from "node:path";

export const name = "generate-image";
export const description =
  "根据文字描述生成图片。非阻塞：提交后立即返回，完成后自动显示。";

export const parameters = {
  type: "object",
  properties: {
    prompt:     { type: "string", description: "图片描述（中英文均可）" },
    count:      { type: "number", description: "并发生成张数，默认 1，最大 9" },
    image:      { type: "string", description: "参考图路径（图生图）" },
    ratio:      { type: "string", description: "长宽比：1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 21:9" },
    resolution: { type: "string", description: "分辨率：2k, 4k（默认 2k）" },
    model:      { type: "string", description: "模型 ID 或简称（如 5.0、dall-e-3）。省略时使用已配置的默认模型" },
    provider:   { type: "string", description: "指定 provider（可选）" },
  },
  required: ["prompt"],
};

async function adapterIsAvailable(adapter, submitCtx) {
  if (typeof adapter?.checkAuth !== "function") return true;
  try {
    const result = await adapter.checkAuth(submitCtx);
    return result?.ok !== false;
  } catch {
    return false;
  }
}

function createTaskId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function errorMessage(err) {
  return err?.message || String(err || "未知错误");
}

function normalizeSessionPath(ctx) {
  const sessionPath = typeof ctx?.sessionPath === "string" ? ctx.sessionPath.trim() : "";
  return sessionPath || null;
}

function bridgeDeliveryTarget(ctx) {
  const bridge = ctx?.bridgeContext;
  if (bridge?.isBridgeSession !== true || !bridge.platform || !bridge.chatId) return null;
  return {
    kind: "bridge",
    platform: bridge.platform,
    chatId: bridge.chatId,
    ...(bridge.sessionKey ? { sessionKey: bridge.sessionKey } : {}),
    ...(bridge.agentId ? { agentId: bridge.agentId } : {}),
    ...(bridge.chatType ? { chatType: bridge.chatType } : {}),
  };
}

export async function resolveImageAdapter(input, registry, submitCtx) {
  if (input.provider) return registry.get(input.provider);

  const defaultProvider = submitCtx.config?.get?.("defaultImageModel")?.provider;
  if (defaultProvider) {
    const adapter = registry.get(defaultProvider);
    if (adapter && await adapterIsAvailable(adapter, submitCtx)) return adapter;
  }

  const adapters = registry.getByType("image");
  for (let i = adapters.length - 1; i >= 0; i--) {
    const adapter = adapters[i];
    if (await adapterIsAvailable(adapter, submitCtx)) return adapter;
  }
  return adapters.at(-1) || null;
}

function markSubmitFailed({ taskId, err, store, ctx }) {
  const message = errorMessage(err);
  store.update(taskId, {
    status: "failed",
    failReason: message,
    submitState: "failed",
    completedAt: new Date().toISOString(),
  });
  ctx.bus.request("deferred:fail", { taskId, error: err }).catch(() => {});
  ctx.bus.request("task:remove", { taskId }).catch(() => {});
  ctx.log?.error?.(`[image-gen] submit failed for ${taskId}:`, message);
}

async function runSubmitInBackground({ taskId, adapter, params, submitCtx, store, poller, ctx }) {
  try {
    const result = await adapter.submit(params, submitCtx);
    const hasProviderTaskId = typeof result?.taskId === "string" && result.taskId.trim();
    const adapterTaskId = hasProviderTaskId ? result.taskId : taskId;
    const files = Array.isArray(result?.files) ? result.files.filter(Boolean) : [];

    if (!hasProviderTaskId && files.length === 0) {
      throw new Error("图片生成 provider 没有返回 taskId 或文件");
    }

    store.update(taskId, {
      submitState: "submitted",
      adapterTaskId,
      ...(files.length ? { files } : {}),
    });

    if (files.length && typeof poller.checkNow === "function") {
      void poller.checkNow(taskId);
    }
  } catch (err) {
    markSubmitFailed({ taskId, err, store, ctx });
  }
}

export async function execute(input, ctx) {
  const { registry, store, poller } = ctx._mediaGen || {};
  if (!registry || !store || !poller) {
    return { content: [{ type: "text", text: "图片生成插件未初始化" }] };
  }

  const sessionPath = normalizeSessionPath(ctx);
  if (!sessionPath) {
    return { content: [{ type: "text", text: "图片生成需要明确的会话归属，当前工具调用缺少 sessionPath" }] };
  }

  // Build adapter context
  const generatedDir = path.join(ctx.dataDir, "generated");
  const submitCtx = { dataDir: ctx.dataDir, bus: ctx.bus, log: ctx.log, generatedDir, config: ctx.config };

  // Resolve adapter: explicit → configured default → latest credentialed adapter.
  const adapter = await resolveImageAdapter(input, registry, submitCtx);
  if (!adapter) {
    return { content: [{ type: "text", text: "没有可用的图片生成 provider" }] };
  }

  const count = Math.min(Math.max(input.count || 1, 1), 9);
  const batchId = createTaskId();

  const params = {
    type: "image",
    prompt: input.prompt,
    ...(input.ratio && { ratio: input.ratio }),
    ...(input.resolution && { resolution: input.resolution }),
    ...(input.model && { model: input.model }),
    ...(input.image && { image: input.image }),
  };

  const submitted = [];
  const deliveryTarget = bridgeDeliveryTarget(ctx);
  const deferredMeta = {
    type: "image-generation",
    mediaKind: "image",
    deliveryIntent: "ui_only",
    triggerParentTurn: false,
    prompt: input.prompt,
    ...(deliveryTarget ? { deliveryTarget } : {}),
  };

  for (let i = 0; i < count; i++) {
    const taskId = createTaskId();
    store.add({
      taskId,
      adapterId: adapter.id,
      batchId,
      type: "image",
      prompt: input.prompt,
      params,
      sessionPath,
      ...(deliveryTarget ? { deliveryTarget } : {}),
      submitState: "submitting",
      adapterTaskId: null,
    });

    // Register deferred notification
    try {
      await ctx.bus.request("deferred:register", {
        taskId,
        sessionPath,
        meta: deferredMeta,
      });
    } catch (err) {
      ctx.log.warn(`deferred:register failed for ${taskId}:`, err);
    }

    // Register in TaskRegistry for visibility and cancellation
    try {
      await ctx.bus.request("task:register", {
        taskId,
        type: "media-generation",
        parentSessionPath: sessionPath,
        meta: deferredMeta,
      });
    } catch {}

    // Add to poller (handles fake-async detection internally)
    poller.add(taskId);
    submitted.push({ taskId });

    void runSubmitInBackground({
      taskId,
      adapter,
      params,
      submitCtx,
      store,
      poller,
      ctx,
    });
  }

  const text = `已提交 ${submitted.length} 张图片生成，完成后会自动显示在下方卡片中。`;

  return {
    content: [{ type: "text", text }],
    details: {
      mediaGeneration: {
        kind: "image",
        batchId,
        prompt: input.prompt,
        tasks: submitted,
      },
    },
  };
}
