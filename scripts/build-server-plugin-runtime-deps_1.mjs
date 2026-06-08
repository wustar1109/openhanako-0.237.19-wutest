import fs from "fs";
import path from "path";
import { nodeFileTrace } from "@vercel/nft";

export const BUNDLED_PLUGIN_ALLOWED_HOST_DIRS = ["core", "lib", "shared"];

const PLUGIN_RUNTIME_EXTENSIONS = new Set([".js", ".mjs", ".cjs"]);
const IGNORED_RUNTIME_DIRS = new Set(["tests", "__tests__", "node_modules"]);

function toPosixPath(value) {
  return value.split(path.sep).join("/");
}

function toNativeRelativePath(value) {
  return value.split("/").join(path.sep);
}

function isPluginRuntimeEntry(relativePath) {
  const parts = relativePath.split(/[\\/]/);
  if (parts.some((part) => IGNORED_RUNTIME_DIRS.has(part))) return false;
  const base = parts.at(-1) || "";
  if (/\.(?:test|spec)\.[cm]?js$/i.test(base)) return false;
  return PLUGIN_RUNTIME_EXTENSIONS.has(path.extname(base));
}

function listPluginRuntimeEntries(rootDir) {
  const pluginsDir = path.join(rootDir, "plugins");
  if (!fs.existsSync(pluginsDir)) return [];
  const entries = [];

  function visit(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const fullPath = path.join(dir, entry.name);
      const relative = path.relative(pluginsDir, fullPath);
      if (entry.isDirectory()) {
        if (IGNORED_RUNTIME_DIRS.has(entry.name)) continue;
        visit(fullPath);
      } else if (entry.isFile() && isPluginRuntimeEntry(relative)) {
        entries.push(fullPath);
      }
    }
  }

  visit(pluginsDir);
  return entries.sort();
}

function assertAllowedHostDependency(relativePath, allowedHostDirs) {
  const topLevelDir = relativePath.split(/[\\/]/)[0];
  if (allowedHostDirs.includes(topLevelDir)) return;
  throw new Error(
    `[build-server] bundled plugin imports unsupported host runtime dependency: ${relativePath}. `
      + `Allowed roots: ${allowedHostDirs.join(", ")}`,
  );
}

export async function collectBundledPluginRuntimeDependencies({
  rootDir,
  allowedHostDirs = BUNDLED_PLUGIN_ALLOWED_HOST_DIRS,
} = {}) {
  if (!rootDir) throw new Error("[build-server] collectBundledPluginRuntimeDependencies requires rootDir");
  const pluginEntries = listPluginRuntimeEntries(rootDir);
  if (pluginEntries.length === 0) return [];

  const { fileList } = await nodeFileTrace(pluginEntries, {
    base: rootDir,
    conditions: ["node", "import"],
    ignore: ["node_modules/**"],
  });

  const dependencies = new Set();
  for (const tracedFile of fileList) {
    const relative = toPosixPath(tracedFile);
    if (!relative || relative === "package.json") continue;
    if (relative.startsWith("plugins/") || relative.startsWith("node_modules/")) continue;

    const nativeRelative = toNativeRelativePath(relative);
    assertAllowedHostDependency(nativeRelative, allowedHostDirs);
    dependencies.add(nativeRelative);
  }

  return [...dependencies].sort();
}

export async function copyBundledPluginRuntimeDependencies({
  rootDir,
  outDir,
  allowedHostDirs = BUNDLED_PLUGIN_ALLOWED_HOST_DIRS,
} = {}) {
  if (!outDir) throw new Error("[build-server] copyBundledPluginRuntimeDependencies requires outDir");
  const dependencies = await collectBundledPluginRuntimeDependencies({ rootDir, allowedHostDirs });

  for (const relativePath of dependencies) {
    const source = path.join(rootDir, relativePath);
    const target = path.join(outDir, relativePath);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(source, target);
  }

  return dependencies;
}
