const { pathToFileUrl } = require("./path-to-file-url.cjs");

const EXPLICIT_PROTOCOL_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;
const SAFE_IMAGE_URL_PROTOCOLS = new Set(["http:", "https:", "file:", "data:"]);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

function normalizePathSeparators(value) {
  return String(value).replace(/\\/g, "/");
}

function dirnamePortable(filePath) {
  const normalized = normalizePathSeparators(filePath);
  const slash = normalized.lastIndexOf("/");
  if (slash < 0) return null;
  if (slash === 0) return "/";
  return normalized.slice(0, slash);
}

function isAbsoluteLocalPath(value) {
  return value.startsWith("/")
    || /^[A-Za-z]:[\\/]/.test(value)
    || value.startsWith("\\\\")
    || value.startsWith("//");
}

function normalizeJoinedPath(pathname) {
  const normalized = normalizePathSeparators(pathname);
  const prefixMatch = normalized.match(/^(?:[A-Za-z]:|\/\/[^/]+\/[^/]+|\/)?/);
  const prefix = prefixMatch?.[0] || "";
  const rest = normalized.slice(prefix.length);
  const parts = [];

  for (const part of rest.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (parts.length > 0 && parts[parts.length - 1] !== "..") {
        parts.pop();
      } else if (!prefix) {
        parts.push(part);
      }
      continue;
    }
    parts.push(part);
  }

  if (!prefix) return parts.join("/");
  if (prefix.endsWith("/")) return `${prefix}${parts.join("/")}`;
  return parts.length ? `${prefix}/${parts.join("/")}` : prefix;
}

function decodeMarkdownPath(rawPath) {
  try {
    return decodeURI(rawPath);
  } catch {
    return rawPath;
  }
}

function splitResourceSuffix(raw) {
  const hash = raw.indexOf("#");
  const query = raw.indexOf("?");
  const indexes = [hash, query].filter(index => index >= 0);
  const splitAt = indexes.length ? Math.min(...indexes) : -1;
  if (splitAt < 0) return { pathname: raw, suffix: "" };
  return { pathname: raw.slice(0, splitAt), suffix: raw.slice(splitAt) };
}

function sanitizeImageUrl(raw) {
  const value = String(raw || "").trim();
  if (!value || !EXPLICIT_PROTOCOL_RE.test(value)) return null;
  try {
    const parsed = new URL(value);
    return SAFE_IMAGE_URL_PROTOCOLS.has(parsed.protocol) ? value : null;
  } catch {
    return null;
  }
}

function resolveLocalImagePath(rawPath, sourceFilePath) {
  const decodedPath = decodeMarkdownPath(String(rawPath || "").trim());
  if (!decodedPath) return null;
  if (isAbsoluteLocalPath(decodedPath)) return normalizeJoinedPath(decodedPath);

  const baseDir = dirnamePortable(sourceFilePath);
  if (!baseDir) return null;
  return normalizeJoinedPath(`${baseDir}/${decodedPath}`);
}

function resolveScreenshotMarkdownImageSrc(src, options = {}) {
  const trimmed = String(src || "").trim();
  if (!trimmed) return src;

  const safeUrl = sanitizeImageUrl(trimmed);
  if (safeUrl) return safeUrl;
  if (EXPLICIT_PROTOCOL_RE.test(trimmed)) return "";
  if (!options.sourceFilePath) return src;

  const { pathname, suffix } = splitResourceSuffix(trimmed);
  const resolvedPath = resolveLocalImagePath(pathname, options.sourceFilePath);
  if (!resolvedPath) return src;
  return `${pathToFileUrl(resolvedPath)}${suffix}`;
}

function decorateScreenshotMarkdownIt(md) {
  const defaultImage = md.renderer.rules.image
    || ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options));

  md.renderer.rules.image = (tokens, idx, options, env, self) => {
    const token = tokens[idx];
    const src = token.attrGet("src");
    if (src) {
      token.attrSet("src", resolveScreenshotMarkdownImageSrc(src, {
        sourceFilePath: env?.sourceFilePath || null,
      }));
    }
    return defaultImage(tokens, idx, options, env, self);
  };
}

function renderScreenshotCodeArticle(source, language) {
  const lang = typeof language === "string" && /^[A-Za-z0-9_+.-]+$/.test(language)
    ? ` class="language-${escapeAttr(language)}"`
    : "";
  return `<pre><code${lang}>${escapeHtml(source)}</code></pre>`;
}

module.exports = {
  decorateScreenshotMarkdownIt,
  escapeAttr,
  renderScreenshotCodeArticle,
  resolveScreenshotMarkdownImageSrc,
};
