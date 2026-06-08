export const MASKED_SECRET = "********";

const DEFAULT_SECRET_KEYS = new Set([
  "api_key",
  "apiKey",
  "token",
  "botToken",
  "appSecret",
  "secret",
  "password",
  "accessToken",
  "refreshToken",
]);

export function isMaskedSecretValue(value) {
  return typeof value === "string" && value.trim() === MASKED_SECRET;
}

export function hasSecretValue(value) {
  return typeof value === "string" && value.length > 0;
}

export function maskSecretValue(value) {
  return hasSecretValue(value) ? MASKED_SECRET : "";
}

export function maskObjectSecrets(value, secretKeys = DEFAULT_SECRET_KEYS) {
  if (!value || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => maskObjectSecrets(item, secretKeys));
  const keySet = normalizeSecretKeySet(secretKeys);
  const out = {};
  for (const [key, entry] of Object.entries(value)) {
    if (keySet.has(key)) {
      out[key] = maskSecretValue(entry);
    } else if (entry && typeof entry === "object") {
      out[key] = maskObjectSecrets(entry, keySet);
    } else {
      out[key] = entry;
    }
  }
  return out;
}

export function resolveSecretPatch({
  patch,
  existing = {},
  secretKeys = DEFAULT_SECRET_KEYS,
}) {
  const source = patch && typeof patch === "object" ? patch : {};
  const saved = existing && typeof existing === "object" ? existing : {};
  const keySet = normalizeSecretKeySet(secretKeys);
  const out = {};
  for (const [key, value] of Object.entries(source)) {
    out[key] = keySet.has(key) && isMaskedSecretValue(value)
      ? saved[key] || ""
      : value;
  }
  return out;
}

export function collectSecretPatchPaths(value, secretKeys = DEFAULT_SECRET_KEYS) {
  const keySet = normalizeSecretKeySet(secretKeys);
  const paths = [];
  collectSecretPatchPathsInto(value, keySet, "", paths);
  return paths;
}

export function hasSecretPatch(value, secretKeys = DEFAULT_SECRET_KEYS) {
  return collectSecretPatchPaths(value, secretKeys).length > 0;
}

function normalizeSecretKeySet(secretKeys) {
  if (secretKeys instanceof Set) return secretKeys;
  if (Array.isArray(secretKeys)) return new Set(secretKeys);
  return DEFAULT_SECRET_KEYS;
}

function collectSecretPatchPathsInto(value, secretKeys, prefix, paths) {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach((entry, index) => {
      collectSecretPatchPathsInto(entry, secretKeys, prefix ? `${prefix}.${index}` : String(index), paths);
    });
    return;
  }
  for (const [key, entry] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (secretKeys.has(key)) {
      if (!isMaskedSecretValue(entry)) paths.push(path);
      continue;
    }
    collectSecretPatchPathsInto(entry, secretKeys, path, paths);
  }
}
