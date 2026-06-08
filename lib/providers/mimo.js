/**
 * Xiaomi MiMo provider plugin
 *
 * 文档：https://dev.mi.com/mimo-open-platform
 */

/** @type {import('../../core/provider-registry.js').ProviderPlugin} */
export const MIMO_LEGACY_BASE_URL = "https://api.xiaomimimo.com/v1";
export const MIMO_TOKEN_PLAN_OPENAI_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1";

export const mimoPlugin = {
  id: "mimo",
  displayName: "Xiaomi (MiMo)",
  authType: "api-key",
  defaultBaseUrl: MIMO_TOKEN_PLAN_OPENAI_BASE_URL,
  defaultApi: "openai-completions",
};
