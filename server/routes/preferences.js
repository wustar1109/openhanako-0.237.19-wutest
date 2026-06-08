/**
 * 全局偏好设置路由（跨 agent 共享）
 *
 * GET  /api/preferences/models  — 读取全局模型 + 搜索配置
 * PUT  /api/preferences/models  — 更新全局模型 + 搜索配置
 * GET  /api/preferences/appearance  — 读取跨前端外观偏好
 * PUT  /api/preferences/appearance  — 更新跨前端外观偏好
 * POST /api/preferences/setup-complete — 提交首次配置完成意图
 * GET  /api/preferences/computer-use  — 读取 Computer Use provider/approval 状态
 * PUT  /api/preferences/computer-use  — 更新 Computer Use 全局设置
 * POST /api/preferences/computer-use/request-permissions — 请求系统权限
 * POST /api/preferences/computer-use/approvals  — 批准 app
 * DELETE /api/preferences/computer-use/approvals  — 撤销批准
 */

import { Hono } from "hono";
import { emitAppEvent } from "../app-events.js";
import { safeJson } from "../hono-helpers.js";
import { debugLog } from "../../lib/debug-log.js";
import { normalizeWorkspacePath } from "../../shared/workspace-history.js";
import {
  normalizeWorkspaceUiEntry,
  normalizeWorkspaceUiSurface,
} from "../../shared/workspace-ui-state.js";
import {
  SEARCH_API_PROVIDER_IDS,
  normalizeSearchApiKeys,
} from "../../shared/search-providers.js";
import {
  normalizeSharedModelsPatch,
  sharedModelsPatchRequiresModelSync,
} from "../../core/config-coordinator.js";
import { modelSupportsImage } from "../../core/message-sanitizer.js";
import {
  effectiveComputerUseSettings,
  isComputerUsePlatformSupported,
  selectedComputerProviderId,
} from "../../core/computer-use/platform-support.js";
import { collectSecretPatchPaths, isMaskedSecretValue, maskSecretValue, resolveSecretPatch } from "../../shared/secret-custody.js";
import { denySecretMutationWithoutScope, denyWithoutScope } from "../http/capability-guard.js";
import { recordSecurityAuditEvent } from "../http/security-audit.js";

function selectedComputerProviderIdFromSettings(settings, platform = process.platform) {
  return selectedComputerProviderId(settings, { platform });
}

function disabledComputerUseStatus(settings, { platform = process.platform } = {}) {
  return {
    enabled: false,
    platform,
    supported: isComputerUsePlatformSupported(platform),
    selectedProviderId: selectedComputerProviderIdFromSettings(settings, platform),
    providers: [],
    activeLease: null,
  };
}

function maskSearchApiKeys(apiKeys) {
  const normalized = normalizeSearchApiKeys(apiKeys);
  return Object.fromEntries(
    Object.entries(normalized).map(([provider, key]) => [provider, maskSecretValue(key)]),
  );
}

function resolveSearchApiKeysPatch(patch, existing) {
  const saved = normalizeSearchApiKeys(existing);
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return saved;
  const out = { ...saved };
  for (const provider of SEARCH_API_PROVIDER_IDS) {
    if (!Object.prototype.hasOwnProperty.call(patch, provider)) continue;
    const value = patch[provider];
    if (isMaskedSecretValue(value)) {
      if (saved[provider]) out[provider] = saved[provider];
      continue;
    }
    if (typeof value !== "string" || !value.trim()) {
      delete out[provider];
      continue;
    }
    out[provider] = value.trim();
  }
  return out;
}

function resolveSearchPreferencePatch(patch, existing) {
  const resolved = resolveSecretPatch({
    patch,
    existing,
    secretKeys: ["api_key"],
  });
  if (patch?.api_keys !== undefined) {
    resolved.api_keys = resolveSearchApiKeysPatch(patch.api_keys, existing?.api_keys || {});
  }
  return resolved;
}

export function createPreferencesRoute(engine, { platform = process.platform } = {}) {
  const route = new Hono();

  // 读取全局模型 + 搜索配置
  route.get("/preferences/models", async (c) => {
    try {
      const models = engine.getSharedModels();
      const search = engine.getSearchConfig();
      const utilityApi = engine.getUtilityApi();

      return c.json({
        models,
        search: {
          provider: search.provider || "",
          api_key: maskSecretValue(search.api_key || ""),
          api_keys: maskSearchApiKeys(search.api_keys || {}),
        },
        utility_api: {
          provider: utilityApi.provider || "",
          base_url: utilityApi.base_url || "",
          api_key: maskSecretValue(utilityApi.api_key || ""),
        },
      });
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  // 更新全局模型 + 搜索配置
  route.put("/preferences/models", async (c) => {
    try {
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const settingsDenied = denyWithoutScope(c, "settings.write");
      if (settingsDenied) return settingsDenied;
      const secretFields = collectSecretPatchPaths(body, ["api_key", "api_keys"]);
      const secretDenied = denySecretMutationWithoutScope(c, secretFields);
      if (secretDenied) return secretDenied;

      const sections = [];
      let needsModelSync = false;
      // 共享模型与辅助视觉开关
      if (body.models) {
        let modelsPatch;
        try {
          modelsPatch = normalizeSharedModelsPatch(body.models);
        } catch (err) {
          return c.json({ error: err.message }, 400);
        }
        if (modelsPatch.vision) {
          let resolved;
          try {
            resolved = engine.resolveModelWithCredentials(modelsPatch.vision);
          } catch (err) {
            return c.json({ error: err.message }, 400);
          }
          if (!modelSupportsImage(resolved?.model)) {
            return c.json({ error: "vision model must support image input" }, 400);
          }
        }
        engine.setSharedModels(modelsPatch);
        sections.push("models");
        needsModelSync = sharedModelsPatchRequiresModelSync(modelsPatch);
      }

      // 搜索配置
      if (body.search) {
        engine.setSearchConfig(resolveSearchPreferencePatch(body.search, engine.getSearchConfig?.() || {}));
        sections.push("search");
      }

      // utility API 配置
      if (body.utility_api) {
        engine.setUtilityApi(resolveSecretPatch({
          patch: body.utility_api,
          existing: engine.getUtilityApi?.() || {},
          secretKeys: ["api_key"],
        }));
        sections.push("utility_api");
      }

      if (needsModelSync) {
        await engine.syncModelsAndRefresh();
      }

      debugLog()?.log("api", `PUT /api/preferences/models sections=[${sections.join(",")}]`);
      if (sections.length > 0) {
        emitAppEvent(engine, "models-changed", { agentId: engine.currentAgentId || null });
      }
      recordSecurityAuditEvent(c, engine, {
        action: "settings.preferences.models.update",
        target: "preferences.models",
        secretFields,
        metadata: { sections },
      });
      return c.json({ ok: true });
    } catch (err) {
      debugLog()?.error("api", `PUT /api/preferences/models failed: ${err.message}`);
      return c.json({ error: err.message }, 500);
    }
  });

  route.get("/preferences/appearance", async (c) => {
    try {
      return c.json({ appearance: engine.getAppearance?.() || {} });
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  route.put("/preferences/appearance", async (c) => {
    try {
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const patch = body.appearance && typeof body.appearance === "object" ? body.appearance : body;
      const before = engine.getAppearance?.() || {};
      const appearance = engine.setAppearance?.(patch) || {};
      emitAppearanceEvents(engine, before, appearance);
      return c.json({ ok: true, appearance });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.post("/preferences/setup-complete", async (c) => {
    try {
      const result = typeof engine.markSetupComplete === "function"
        ? engine.markSetupComplete()
        : engine.preferences?.markSetupComplete?.();
      if (result?.setupComplete !== true) {
        throw new Error("setup completion manager is unavailable");
      }
      return c.json({ ok: true, setupComplete: true });
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  route.get("/preferences/workspace-ui-state", async (c) => {
    try {
      const workspace = normalizeWorkspacePath(c.req.query("workspace"));
      if (!workspace) return c.json({ error: "workspace must be a non-empty path" }, 400);
      const surface = normalizeWorkspaceUiSurface(c.req.query("surface"));
      if (!surface) return c.json({ error: "workspace UI surface is invalid" }, 400);
      if (typeof engine.getWorkspaceUiState !== "function") {
        return c.json({ error: "workspace UI state unavailable" }, 500);
      }
      return c.json({ state: engine.getWorkspaceUiState(workspace, surface) });
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  route.put("/preferences/workspace-ui-state", async (c) => {
    try {
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const workspace = normalizeWorkspacePath(body.workspace);
      if (!workspace) return c.json({ error: "workspace must be a non-empty path" }, 400);
      const surface = normalizeWorkspaceUiSurface(body.surface);
      if (!surface) return c.json({ error: "workspace UI surface is invalid" }, 400);
      if (typeof engine.setWorkspaceUiState !== "function") {
        return c.json({ error: "workspace UI state unavailable" }, 500);
      }
      const state = engine.setWorkspaceUiState(workspace, surface, normalizeWorkspaceUiEntry(body.state || {}));
      return c.json({ ok: true, state });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.get("/preferences/plugin-ui", async (c) => {
    try {
      return c.json(engine.getPluginUiPrefs());
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  route.put("/preferences/plugin-ui", async (c) => {
    try {
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const result = engine.setPluginUiPrefs(body);
      return c.json({ ok: true, ...result });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.get("/preferences/computer-use", async (c) => {
    try {
      const settings = effectiveComputerUseSettings(engine.getComputerUseSettings(), { platform });
      if (settings?.enabled !== true) {
        const status = disabledComputerUseStatus(settings, { platform });
        return c.json({
          settings,
          status,
          selectedProviderId: status.selectedProviderId,
        });
      }
      const status = await engine.getComputerHost?.()?.getStatus?.({}) || null;
      return c.json({
        settings,
        status,
        selectedProviderId: status?.selectedProviderId || selectedComputerProviderIdFromSettings(settings, platform),
      });
    } catch (err) {
      return c.json({ error: err.message }, 500);
    }
  });

  route.put("/preferences/computer-use", async (c) => {
    try {
      if (!isComputerUsePlatformSupported(platform)) {
        return c.json({ error: "Computer Use is not supported on Linux Preview." }, 400);
      }
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const nextSettings = body.settings && typeof body.settings === "object" ? body.settings : body;
      const settings = typeof engine.updateComputerUseSettings === "function"
        ? await engine.updateComputerUseSettings(nextSettings)
        : engine.setComputerUseSettings(nextSettings);
      debugLog()?.log("api", "PUT /api/preferences/computer-use");
      emitAppEvent(engine, "computer-use-settings-changed", { selectedProviderId: selectedComputerProviderIdFromSettings(settings, platform) });
      return c.json({ ok: true, settings });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.post("/preferences/computer-use/request-permissions", async (c) => {
    try {
      if (!isComputerUsePlatformSupported(platform)) {
        return c.json({ error: "Computer Use is not supported on Linux Preview." }, 400);
      }
      const body = await safeJson(c);
      const providerId = body && typeof body === "object" ? body.providerId || null : null;
      const result = await engine.getComputerHost?.()?.requestPermissions?.({}, providerId);
      emitAppEvent(engine, "computer-use-permissions-requested", { providerId: providerId || result?.providerId || null });
      return c.json({ ok: true, result });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.post("/preferences/computer-use/approvals", async (c) => {
    try {
      if (!isComputerUsePlatformSupported(platform)) {
        return c.json({ error: "Computer Use is not supported on Linux Preview." }, 400);
      }
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const settings = engine.approveComputerUseApp(body);
      emitAppEvent(engine, "computer-use-settings-changed", { providerId: body.providerId || null, appId: body.appId || null });
      return c.json({ ok: true, settings });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  route.delete("/preferences/computer-use/approvals", async (c) => {
    try {
      if (!isComputerUsePlatformSupported(platform)) {
        return c.json({ error: "Computer Use is not supported on Linux Preview." }, 400);
      }
      const body = await safeJson(c);
      if (!body || typeof body !== "object") {
        return c.json({ error: "invalid JSON body" }, 400);
      }
      const settings = engine.revokeComputerUseApp(body);
      emitAppEvent(engine, "computer-use-settings-changed", { providerId: body.providerId || null, appId: body.appId || null });
      return c.json({ ok: true, settings });
    } catch (err) {
      return c.json({ error: err.message }, 400);
    }
  });

  return route;
}

function emitAppearanceEvents(engine, before, appearance) {
  if (appearance.theme && appearance.theme !== before.theme) {
    emitAppEvent(engine, "theme-changed", { theme: appearance.theme });
  }
  if (typeof appearance.serif === "boolean" && appearance.serif !== before.serif) {
    emitAppEvent(engine, "font-changed", { serif: appearance.serif });
  }
  if (typeof appearance.paperTexture === "boolean" && appearance.paperTexture !== before.paperTexture) {
    emitAppEvent(engine, "paper-texture-changed", { enabled: appearance.paperTexture });
  }
  if (typeof appearance.leavesOverlay === "boolean" && appearance.leavesOverlay !== before.leavesOverlay) {
    emitAppEvent(engine, "leaves-overlay-changed", { enabled: appearance.leavesOverlay });
  }
}
