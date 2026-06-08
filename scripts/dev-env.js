import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function defaultProjectRoot() {
  return resolve(fileURLToPath(new URL("..", import.meta.url)));
}

function sanitizePathSegment(value) {
  const segment = String(value || "project")
    .replace(/^\.+/, "")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return segment || "project";
}

function projectRootHash(projectRoot) {
  const normalized = process.platform === "win32"
    ? resolve(projectRoot).toLowerCase()
    : resolve(projectRoot);
  return createHash("sha256").update(normalized).digest("hex").slice(0, 10);
}

export function defaultDevHanaHome({
  projectRoot = defaultProjectRoot(),
} = {}) {
  const root = resolve(projectRoot);
  const slug = sanitizePathSegment(basename(root));
  return join(homedir(), ".hanako-dev", `${slug}-${projectRootHash(root)}`);
}

export function applyDevEnvironment(env = process.env, {
  nodeBin = process.execPath,
  projectRoot = defaultProjectRoot(),
} = {}) {
  env.HANA_HOME = defaultDevHanaHome({ projectRoot });
  env.HANA_DEV_NODE_BIN = nodeBin;
  if (!env.HANA_PORT) env.HANA_PORT = "0";
  return env;
}
