export const AUTO_SEARCH_PROVIDER = "auto";

export const SEARCH_API_PROVIDER_IDS = Object.freeze([
  "tavily",
  "brave",
  "serper",
]);

export const BROWSER_SEARCH_PROVIDER_IDS = Object.freeze([
  "bing_browser",
  "google_browser",
  "duckduckgo_browser",
]);

const SEARCH_API_PROVIDER_SET = new Set(SEARCH_API_PROVIDER_IDS);
const BROWSER_SEARCH_PROVIDER_SET = new Set(BROWSER_SEARCH_PROVIDER_IDS);

export function normalizeSearchProvider(provider) {
  return String(provider || "").trim();
}

export function isSearchApiProvider(provider) {
  return SEARCH_API_PROVIDER_SET.has(normalizeSearchProvider(provider));
}

export function isBrowserSearchProvider(provider) {
  return BROWSER_SEARCH_PROVIDER_SET.has(normalizeSearchProvider(provider));
}

export function isKnownSearchProvider(provider) {
  const normalized = normalizeSearchProvider(provider);
  return normalized === AUTO_SEARCH_PROVIDER
    || isSearchApiProvider(normalized)
    || isBrowserSearchProvider(normalized);
}

export function normalizeSearchApiKeys(raw) {
  const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const out = {};
  for (const provider of SEARCH_API_PROVIDER_IDS) {
    const value = source[provider];
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed) out[provider] = trimmed;
  }
  return out;
}

export function mergeSearchApiKeys(base, patch) {
  const out = normalizeSearchApiKeys(base);
  const source = patch && typeof patch === "object" && !Array.isArray(patch) ? patch : {};
  for (const provider of SEARCH_API_PROVIDER_IDS) {
    if (!Object.prototype.hasOwnProperty.call(source, provider)) continue;
    const value = source[provider];
    if (value === null || value === undefined || value === "") {
      delete out[provider];
      continue;
    }
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) out[provider] = trimmed;
      else delete out[provider];
    }
  }
  return out;
}
