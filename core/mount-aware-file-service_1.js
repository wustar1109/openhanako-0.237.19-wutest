import crypto from "crypto";
import fs from "fs";
import path from "path";
import { loadStudioMountRegistry } from "./studio-mounts.js";
import { createModuleLogger } from "../lib/debug-log.js";

const log = createModuleLogger("mount-files");

const SEARCH_SKIP_DIRS = new Set([
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  ".next",
  ".turbo",
  "dist",
  "build",
  "coverage",
]);
const SEARCH_LIMIT = 80;

export class MountAwareFileError extends Error {
  constructor(message, { code = "file_action_failed", status = 400 } = {}) {
    super(message);
    this.name = "MountAwareFileError";
    this.code = code;
    this.status = status;
  }
}

export class MountAwareFileService {
  constructor({
    hanakoHome,
    defaultRoot,
    studioId,
    createCheckpoint,
  } = {}) {
    if (!hanakoHome) throw new Error("hanakoHome required");
    this._hanakoHome = hanakoHome;
    this._defaultRoot = defaultRoot || null;
    this._studioId = studioId || null;
    this._createCheckpoint = typeof createCheckpoint === "function" ? createCheckpoint : null;
  }

  resolveRoot(rootId = "default") {
    return publicRoot(this._resolveRootInternal(rootId));
  }

  resolveDirectory(rootId = "default", subdir = "") {
    const root = this._resolveRootInternal(rootId);
    const normalized = normalizeSubdirOrThrow(subdir);
    const dir = resolveInsideRoot(root.path, normalized);
    if (!dir) throw fileError("invalid path", "invalid_path", 400);
    return dir;
  }

  async listFiles(rootId = "default", subdir = "") {
    const root = this._resolveRootInternal(rootId);
    requireCapability(root, "list");
    const normalized = normalizeSubdirOrThrow(subdir);
    const dir = resolveInsideRoot(root.path, normalized);
    if (!dir) throw fileError("invalid path", "invalid_path", 400);
    return {
      rootId: root.id,
      subdir: normalized,
      files: await listFiles(dir),
    };
  }

  async searchFiles(rootId = "default", query = "") {
    const root = this._resolveRootInternal(rootId);
    requireCapability(root, "list");
    const q = String(query || "").trim();
    return {
      rootId: root.id,
      query: q,
      results: q ? await searchFiles(root.path, q) : [],
    };
  }

  contentTarget(rootId = "default", subdir = "", name) {
    const root = this._resolveRootInternal(rootId);
    requireCapability(root, "read");
    const normalized = normalizeSubdirOrThrow(subdir);
    const filename = normalizePlainNameOrThrow(name);
    const dir = resolveInsideRoot(root.path, normalized);
    if (!dir) throw fileError("invalid path", "invalid_path", 400);
    const filePath = resolveFileTarget(root.path, dir, filename);
    if (!filePath) throw fileError("invalid path", "invalid_path", 400);
    return { root, filePath, filename };
  }

  async mkdir(rootId, subdir, body = {}) {
    const { root, dir } = this._writeDir(rootId, subdir);
    const name = normalizePlainNameOrThrow(body.name);
    const target = resolveFileTarget(root.path, dir, name);
    if (!target) throw fileError("invalid path", "invalid_path", 400);
    fs.mkdirSync(target, { recursive: false });
    return { ok: true, action: "mkdir", rootId: root.id, files: await listFiles(dir) };
  }

  async writeText(rootId, subdir, body = {}) {
    const { root, dir } = this._writeDir(rootId, subdir);
    const name = normalizePlainNameOrThrow(body.name);
    const target = resolveFileTarget(root.path, dir, name);
    if (!target) throw fileError("invalid path", "invalid_path", 400);
    if (fs.existsSync(target) && this._createCheckpoint) {
      await this._createCheckpoint({ filePath: target, reason: "mobile-workbench-edit" }).catch(() => null);
    }
    fs.writeFileSync(target, String(body.content ?? ""), "utf-8");
    return { ok: true, action: body.action, rootId: root.id, files: await listFiles(dir) };
  }

  async rename(rootId, subdir, body = {}) {
    const { root, dir } = this._writeDir(rootId, subdir);
    const oldName = normalizePlainNameOrThrow(body.oldName);
    const newName = normalizePlainNameOrThrow(body.newName);
    const source = resolveFileTarget(root.path, dir, oldName);
    const target = resolveFileTarget(root.path, dir, newName);
    if (!source || !target) throw fileError("invalid path", "invalid_path", 400);
    fs.renameSync(source, target);
    return { ok: true, action: "rename", rootId: root.id, files: await listFiles(dir) };
  }

  async move(rootId, subdir, body = {}) {
    const { root, dir } = this._writeDir(rootId, subdir);
    const name = normalizePlainNameOrThrow(body.name);
    const destSubdir = normalizeSubdirOrThrow(body.destSubdir || "");
    const source = resolveFileTarget(root.path, dir, name);
    const destDir = resolveInsideRoot(root.path, destSubdir);
    if (!source || !destDir) throw fileError("invalid path", "invalid_path", 400);
    fs.mkdirSync(destDir, { recursive: true });
    fs.renameSync(source, path.join(destDir, name));
    return { ok: true, action: "move", rootId: root.id, files: await listFiles(dir) };
  }

  async safeDelete(rootId, subdir, body = {}) {
    const { root, dir, normalizedSubdir } = this._writeDir(rootId, subdir);
    const name = normalizePlainNameOrThrow(body.name);
    const source = resolveFileTarget(root.path, dir, name);
    if (!source || !fs.existsSync(source)) throw fileError("file not found", "file_not_found", 404);
    const trashId = `trash_${Date.now()}_${crypto.randomBytes(4).toString("hex")}`;
    const trashDir = path.join(this._hanakoHome, "trash", "mobile-workbench", trashId);
    fs.mkdirSync(trashDir, { recursive: true });
    const payloadPath = path.join(trashDir, "payload");
    fs.renameSync(source, payloadPath);
    fs.writeFileSync(path.join(trashDir, "metadata.json"), JSON.stringify({
      schemaVersion: 1,
      trashId,
      rootId: root.id,
      originalName: name,
      originalSubdir: normalizedSubdir,
      deletedAt: new Date().toISOString(),
    }, null, 2) + "\n", "utf-8");
    return { ok: true, action: "safeDelete", rootId: root.id, trashId, files: await listFiles(dir) };
  }

  writeFileTarget(rootId, subdir, name) {
    const { root, dir } = this._writeDir(rootId, subdir);
    const filename = normalizePlainNameOrThrow(name);
    const target = resolveFileTarget(root.path, dir, filename);
    if (!target) throw fileError("invalid path", "invalid_path", 400);
    return { root, dir, filename, target };
  }

  async filesForDirectory(rootId, subdir) {
    return (await this.listFiles(rootId, subdir)).files;
  }

  _writeDir(rootId = "default", subdir = "") {
    const root = this._resolveRootInternal(rootId);
    requireCapability(root, "write");
    const normalizedSubdir = normalizeSubdirOrThrow(subdir);
    const dir = resolveInsideRoot(root.path, normalizedSubdir);
    if (!dir) throw fileError("invalid path", "invalid_path", 400);
    fs.mkdirSync(dir, { recursive: true });
    return { root, dir, normalizedSubdir };
  }

  _resolveRootInternal(rootId = "default") {
    const id = typeof rootId === "string" && rootId.trim() ? rootId.trim() : "default";
    if (id === "default") {
      if (!this._defaultRoot) throw fileError("no workspace", "no_workspace", 400);
      fs.mkdirSync(this._defaultRoot, { recursive: true });
      return {
        id: "default",
        label: "Default",
        path: this._defaultRoot,
        capabilities: ["list", "read", "write"],
        sourceKind: "storage",
        provider: "local_fs",
      };
    }
    const mount = findLocalFsMount(this._hanakoHome, this._studioId, id);
    if (!mount) throw fileError("unknown root", "unknown_root", 404);
    const rootPath = mount.rootLocator?.path;
    if (typeof rootPath !== "string" || !path.isAbsolute(rootPath)) {
      throw fileError("invalid mount root", "invalid_mount_root", 400);
    }
    fs.mkdirSync(rootPath, { recursive: true });
    return {
      id: mount.mountId,
      mountId: mount.mountId,
      label: mount.label,
      path: rootPath,
      capabilities: mount.capabilities,
      sourceKind: mount.sourceKind,
      provider: mount.provider,
    };
  }
}

function findLocalFsMount(hanakoHome, studioId, rootId) {
  if (!studioId) return null;
  let registry;
  try {
    registry = loadStudioMountRegistry(hanakoHome);
  } catch {
    return null;
  }
  return registry.mounts.find((mount) => mount.mountId === rootId
    && mount.hostStudioId === studioId
    && mount.status === "active"
    && mount.sourceKind === "storage"
    && mount.provider === "local_fs") || null;
}

function publicRoot(root) {
  const { path: _path, ...safe } = root;
  return safe;
}

function requireCapability(root, capability) {
  if (!root.capabilities?.includes(capability)) {
    throw fileError("mount capability denied", "mount_capability_denied", 403);
  }
}

async function listFiles(dir) {
  let entries;
  try {
    entries = await fs.promises.readdir(dir, { withFileTypes: true });
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }
  const items = [];
  for (const entry of entries) {
    if (entry.name.startsWith(".")) continue;
    const fullPath = path.join(dir, entry.name);
    try {
      const stat = await fs.promises.stat(fullPath);
      items.push({
        name: entry.name,
        isDir: entry.isDirectory(),
        size: entry.isDirectory() ? null : stat.size,
        mtime: stat.mtime.toISOString(),
      });
    } catch (err) {
      if (err.code !== "ENOENT") log.warn(`stat failed: ${err.message}`);
    }
  }
  return items.sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
    return a.name.localeCompare(b.name, "zh");
  });
}

async function searchFiles(rootPath, query) {
  const needle = query.toLowerCase();
  const results = [];
  async function walk(dir) {
    if (results.length >= SEARCH_LIMIT) return;
    let entries;
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name, "zh"));
    for (const entry of entries) {
      if (results.length >= SEARCH_LIMIT) break;
      if (entry.name.startsWith(".")) continue;
      if (entry.isDirectory() && SEARCH_SKIP_DIRS.has(entry.name)) continue;
      const fullPath = path.join(dir, entry.name);
      const relativePath = toPortableRelative(rootPath, fullPath);
      const parentSubdir = toPortableRelative(rootPath, path.dirname(fullPath));
      if (entry.name.toLowerCase().includes(needle)) {
        try {
          const stat = await fs.promises.stat(fullPath);
          results.push({
            name: entry.name,
            relativePath,
            parentSubdir,
            isDir: entry.isDirectory(),
            size: entry.isDirectory() ? null : stat.size,
            mtime: stat.mtime.toISOString(),
          });
        } catch {}
      }
      if (entry.isDirectory()) await walk(fullPath);
    }
  }
  await walk(rootPath);
  return results.slice(0, SEARCH_LIMIT);
}

function resolveInsideRoot(rootPath, subdir) {
  const rootReal = realPath(rootPath);
  if (!rootReal) return null;
  const target = subdir ? path.join(rootPath, subdir) : rootPath;
  const targetReal = realPath(target);
  if (targetReal) {
    return targetReal === rootReal || targetReal.startsWith(rootReal + path.sep) ? targetReal : null;
  }
  const parentReal = realPath(path.dirname(target));
  if (!parentReal) return null;
  const full = path.join(parentReal, path.basename(target));
  return full === rootReal || full.startsWith(rootReal + path.sep) ? full : null;
}

function resolveFileTarget(rootPath, dir, name) {
  const target = path.join(dir, name);
  const rootReal = realPath(rootPath);
  if (!rootReal) return null;
  const resolved = realPath(target);
  if (resolved) return resolved === rootReal || resolved.startsWith(rootReal + path.sep) ? resolved : null;
  const parentReal = realPath(path.dirname(target));
  if (!parentReal) return null;
  const full = path.join(parentReal, path.basename(target));
  return full === rootReal || full.startsWith(rootReal + path.sep) ? full : null;
}

function normalizeSubdirOrThrow(value) {
  const raw = String(value || "").replace(/^\/+|\/+$/g, "");
  if (!raw) return "";
  if (raw.includes("\\") || raw.split("/").some((part) => part === ".." || part === "." || part.startsWith("."))) {
    throw fileError("invalid_subdir", "invalid_subdir", 400);
  }
  return raw;
}

function normalizePlainNameOrThrow(value) {
  const name = String(value || "").trim();
  if (!name || name.includes("/") || name.includes("\\") || name === "." || name === ".." || name.startsWith(".")) {
    throw fileError("invalid name", "invalid_name", 400);
  }
  return name;
}

function toPortableRelative(root, target) {
  return path.relative(root, target).split(path.sep).filter(Boolean).join("/");
}

function fileError(message, code, status) {
  return new MountAwareFileError(message, { code, status });
}

function realPath(p) {
  try {
    return fs.realpathSync(path.resolve(p));
  } catch {
    return null;
  }
}
