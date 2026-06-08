/**
 * sandbox/index.js — 沙盒入口（无状态工厂）
 *
 * 每次 buildTools 调用时创建 session 级的 PathGuard + OS 沙盒 exec。
 * 不持有 engine 级状态，天然支持多 agent 并发。
 */

import { deriveSandboxPolicy } from "./policy.js";
import { PathGuard } from "./path-guard.js";
import { detectPlatform, checkAvailability } from "./platform.js";
import { createSeatbeltExec } from "./seatbelt.js";
import { createBwrapExec } from "./bwrap.js";
import { createWin32Exec } from "./win32-exec.js";
import { wrapPathTool, wrapBashTool } from "./tool-wrapper.js";
import { createEnhancedReadFile } from "./read-enhanced.js";
import { wrapReadImageWithVisionBridge } from "./read-image-vision.js";
import { wrapReadOfficeMedia } from "./read-office-media.js";
import { createManagedConfigWriteGuard } from "./managed-config-guard.js";
import { t } from "../../server/i18n.js";
import fs, { constants } from "fs";
import { access as fsAccess } from "fs/promises";
import path, { extname } from "path";
import {
  createReadTool,
  createWriteTool,
  createEditTool,
  createBashTool,
  createGrepTool,
  createFindTool,
  createLsTool,
} from "../pi-sdk/index.js";
import { normalizeWin32ShellPath } from "./win32-path.js";
import { serializeSessionFile } from "../session-files/session-file-response.js";

/**
 * 为一个 session 创建沙盒包装后的工具集
 *
 * 每次调用独立，不共享状态。
 * 当传入 getSandboxEnabled 回调时，工具在每次调用时动态检查沙盒状态，
 * 切换偏好后无需重建 session 即可生效。
 *
 * @param {string} cwd  工作目录
 * @param {object[]} customTools  自定义工具
 * @param {object} opts
 * @param {string} opts.agentDir
 * @param {string|null} opts.workspace
 * @param {string[]} [opts.workspaceFolders]
 * @param {string} opts.hanakoHome
 * @param {() => boolean} opts.getSandboxEnabled  动态沙盒开关（每次工具调用时求值）
 * @param {() => boolean} [opts.getSandboxNetworkEnabled]  动态沙盒联网开关（仅沙盒开启时生效）
 * @param {() => string[]} [opts.getExternalReadPaths]  当前 session 用户显式给过的外部只读路径
 * @param {() => string|null} [opts.getSessionPath]  当前工具调用归属的 sessionPath
 * @param {(entry: object) => void} [opts.recordFileOperation]  记录 write/edit 触达的 session file
 * @param {() => object|null} [opts.getVisionBridge]  辅助视觉桥
 * @param {() => boolean} [opts.isVisionAuxiliaryEnabled]  辅助视觉开关
 * @param {object|null} [opts.legacyCleanupQueue] Windows 旧 ACL 清理队列
 * @returns {{ tools: object[], customTools: object[] }}
 */
export function createSandboxedTools(cwd, customTools, {
  agentDir,
  workspace,
  workspaceFolders = [],
  hanakoHome,
  getSandboxEnabled,
  getSandboxNetworkEnabled,
  getExternalReadPaths,
  getSessionPath,
  recordFileOperation,
  getVisionBridge,
  isVisionAuxiliaryEnabled,
  legacyCleanupQueue = null,
}) {
  // 始终按 standard 模式构建策略和 PathGuard，wrappers 在运行时动态 bypass
  const policy = deriveSandboxPolicy({ agentDir, cwd, workspace, workspaceFolders, hanakoHome, mode: "standard" });
  const guard = new PathGuard(policy);

  // 增强 readFile：xlsx 解析 + 编码检测，保留 PI SDK 默认的 access / detectImageMimeType
  const IMAGE_MIMES = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp" };
  const readOps = {
    readFile: createEnhancedReadFile(),
    access: (p) => fsAccess(p, constants.R_OK),
    detectImageMimeType: async (p) => IMAGE_MIMES[extname(p).toLowerCase()] || undefined,
  };

  const platform = detectPlatform();
  const isWin32 = process.platform === "win32";
  const checkManagedConfigWrite = createManagedConfigWriteGuard({ hanakoHome });
  const wrapOpts = { getSandboxEnabled, getExternalReadPaths, checkManagedConfigWrite };
  const resolveSandboxNetworkEnabled = typeof getSandboxNetworkEnabled === "function"
    ? getSandboxNetworkEnabled
    : () => true;

  // 无 OS 沙盒时的 bash 工具（沙盒关闭时回退用）
  const normalBashTool = isWin32
    ? createBashTool(cwd, { operations: { exec: createWin32Exec() } })
    : createBashTool(cwd);

  const bashWrapOpts = { getSandboxEnabled, getExternalReadPaths, fallbackTool: normalBashTool, checkManagedConfigWrite };
  const writeTool = wrapFileTouchTool(createWriteTool(cwd), cwd, {
    origin: "agent_write",
    operationForPath: (filePath) => fs.existsSync(filePath) ? "modified" : "created",
    getSessionPath,
    recordFileOperation,
  });
  const editTool = wrapFileTouchTool(createEditTool(cwd), cwd, {
    origin: "agent_edit",
    operationForPath: () => "modified",
    getSessionPath,
    recordFileOperation,
  });
  const readTool = wrapReadImageWithVisionBridge(wrapReadOfficeMedia(createReadTool(cwd, { operations: readOps }), cwd, {
    hanakoHome,
    getSessionPath,
    recordFileOperation,
    getVisionBridge,
    isVisionAuxiliaryEnabled,
  }), cwd, {
    getSessionPath,
    recordFileOperation,
    getVisionBridge,
    isVisionAuxiliaryEnabled,
  });

  // ── Windows: PathGuard 包装 + restricted-token exec，关闭沙盒时走 direct fallback ──
  if (platform === "win32-restricted-token") {
    const sandboxedBashTool = createBashTool(cwd, {
      operations: {
        exec: createWin32Exec({
          sandbox: {
            policy,
            hanakoHome,
            getExternalReadPaths,
            getSandboxNetworkEnabled: resolveSandboxNetworkEnabled,
            legacyCleanupQueue,
          },
        }),
      },
    });
    return {
      tools: [
        wrapPathTool(readTool, guard, "read", cwd, wrapOpts),
        wrapPathTool(writeTool, guard, "write", cwd, wrapOpts),
        wrapPathTool(editTool, guard, "write", cwd, wrapOpts),
        wrapBashTool(sandboxedBashTool, guard, cwd, bashWrapOpts),
        wrapPathTool(createGrepTool(cwd), guard, "read", cwd, wrapOpts),
        wrapPathTool(createFindTool(cwd), guard, "read", cwd, wrapOpts),
        wrapPathTool(createLsTool(cwd), guard, "read", cwd, wrapOpts),
      ],
      customTools,
    };
  }

  // ── macOS / Linux: PathGuard + OS 沙盒 ──
  let sandboxedBashTool = normalBashTool;
  if (checkAvailability(platform)) {
    const sandboxExec = platform === "seatbelt"
      ? createSeatbeltExec(policy, { getSandboxNetworkEnabled: resolveSandboxNetworkEnabled })
      : createBwrapExec(policy, { getExternalReadPaths, getSandboxNetworkEnabled: resolveSandboxNetworkEnabled });
    sandboxedBashTool = createBashTool(cwd, { operations: { exec: sandboxExec } });
  } else if (platform === "bwrap") {
    sandboxedBashTool = {
      ...normalBashTool,
      execute: async () => ({
        content: [{ type: "text", text: t("sandbox.osRequired", { platform }) }],
      }),
    };
  }

  return {
    tools: [
      wrapPathTool(readTool, guard, "read", cwd, wrapOpts),
      wrapPathTool(writeTool, guard, "write", cwd, wrapOpts),
      wrapPathTool(editTool, guard, "write", cwd, wrapOpts),
      wrapBashTool(sandboxedBashTool, guard, cwd, bashWrapOpts),
      wrapPathTool(createGrepTool(cwd), guard, "read", cwd, wrapOpts),
      wrapPathTool(createFindTool(cwd), guard, "read", cwd, wrapOpts),
      wrapPathTool(createLsTool(cwd), guard, "read", cwd, wrapOpts),
    ],
    customTools,
  };
}

function resolveToolPath(rawPath, cwd) {
  if (!rawPath) return null;
  if (process.platform === "win32") {
    return normalizeWin32ShellPath(rawPath, cwd, { allowRelative: true });
  }
  return path.isAbsolute(rawPath) ? rawPath : path.resolve(cwd, rawPath);
}

function wrapFileTouchTool(tool, cwd, {
  origin,
  operationForPath,
  getSessionPath,
  recordFileOperation,
} = {}) {
  return {
    ...tool,
    execute: async (toolCallId, params, ...rest) => {
      const absolutePath = resolveToolPath(params?.path, cwd);
      const operation = absolutePath ? operationForPath?.(absolutePath) : null;
      const result = await tool.execute(toolCallId, params, ...rest);
      const sessionPath = getSessionPath?.() || null;
      if (!absolutePath || !sessionPath || typeof recordFileOperation !== "function") {
        return result;
      }
      if (!fs.existsSync(absolutePath)) return result;
      try {
        const sessionFile = serializeSessionFile(recordFileOperation({
          sessionPath,
          filePath: absolutePath,
          label: path.basename(absolutePath),
          origin,
          operation,
        }));
        return appendSessionFileDetails(result, sessionFile);
      } catch (err) {
        return appendRegistrationWarning(result, err);
      }
    },
  };
}

function appendSessionFileDetails(result, sessionFile) {
  if (!sessionFile) return result;
  return {
    ...(result || {}),
    details: {
      ...(result?.details || {}),
      sessionFile,
    },
  };
}

function appendRegistrationWarning(result, err) {
  const message = `Session file registration failed: ${err?.message || String(err)}`;
  const content = Array.isArray(result?.content) ? [...result.content] : [];
  return {
    ...(result || {}),
    content: [...content, { type: "text", text: message }],
  };
}
