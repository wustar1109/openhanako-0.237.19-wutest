import fs from "fs";
import path from "path";
import { Hono } from "hono";
import { MountAwareFileError, MountAwareFileService } from "../../core/mount-aware-file-service.js";
import {
  consumeRemoteWriteLease,
  issueRemoteWriteLease,
  revokeRemoteWriteLease,
} from "../../core/execution-lease-service.js";
import { safeJson } from "../hono-helpers.js";
import { serveFileContent } from "../http/file-content.js";
import { createRequestContext } from "../http/boundary.js";
import { recordSecurityAuditEvent } from "../http/security-audit.js";

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export function createMobileWorkbenchRoute(engine) {
  const route = new Hono();

  route.get("/mobile/bootstrap", (c) => {
    return c.json({
      locale: engine.getLocale?.() || engine.config?.locale || "zh-CN",
      agentName: engine.agentName || "Hanako",
      userName: engine.userName || "User",
      currentAgentId: engine.currentAgentId || null,
      agentYuan: engine.agent?.config?.agent?.yuan || "hanako",
      homeFolder: engine.homeCwd || null,
      cwdHistory: Array.isArray(engine.config?.cwd_history) ? engine.config.cwd_history : [],
      memoryMasterEnabled: engine.agent?.memoryMasterEnabled !== false,
      avatars: readAvatarAvailability(engine),
      agents: typeof engine.listAgents === "function" ? sanitizeAgents(engine.listAgents()) : [],
      appearance: engine.getAppearance?.() || {},
    });
  });

  route.get("/mobile/workbench/files", async (c) => {
    try {
      const auth = authorizeWorkbench(c, engine, "files.read");
      if (auth.response) return auth.response;
      return c.json(await fileService(engine, auth.requestContext)
        .listFiles(c.req.query("rootId"), c.req.query("subdir") || ""));
    } catch (err) {
      return workbenchError(c, err);
    }
  });

  route.get("/mobile/workbench/search", async (c) => {
    try {
      const auth = authorizeWorkbench(c, engine, "files.read");
      if (auth.response) return auth.response;
      return c.json(await fileService(engine, auth.requestContext)
        .searchFiles(c.req.query("rootId"), c.req.query("q") || ""));
    } catch (err) {
      return workbenchError(c, err);
    }
  });

  route.get("/mobile/workbench/content", (c) => serveContent(c, engine, false));
  route.on("HEAD", "/mobile/workbench/content", (c) => serveContent(c, engine, true));

  route.post("/mobile/workbench/actions", async (c) => {
    const auth = authorizeWorkbench(c, engine, "files.write");
    if (auth.response) return auth.response;
    const body = await safeJson(c);
    const files = fileService(engine, auth.requestContext);
    const rootId = body.rootId || "default";
    const subdir = body.subdir || "";

    try {
      switch (body.action) {
        case "mkdir":
          return await writeActionResponse(c, engine, "mobile_workbench.mkdir", auth, rootId, () => files.mkdir(rootId, subdir, body));
        case "create":
        case "writeText":
          return await writeActionResponse(c, engine, "mobile_workbench.write", auth, rootId, () => files.writeText(rootId, subdir, body));
        case "rename":
          return await writeActionResponse(c, engine, "mobile_workbench.rename", auth, rootId, () => files.rename(rootId, subdir, body));
        case "move":
          return await writeActionResponse(c, engine, "mobile_workbench.move", auth, rootId, () => files.move(rootId, subdir, body));
        case "safeDelete":
          return await writeActionResponse(c, engine, "mobile_workbench.safe_delete", auth, rootId, () => files.safeDelete(rootId, subdir, body));
        default:
          return c.json({ error: "unknown_action" }, 400);
      }
    } catch (err) {
      return workbenchError(c, err);
    }
  });

  route.post("/mobile/workbench/upload", async (c) => {
    const auth = authorizeWorkbench(c, engine, "files.write");
    if (auth.response) return auth.response;
    const body = await safeJson(c);
    const filesService = fileService(engine, auth.requestContext);
    const rootId = body.rootId || "default";
    const subdir = body.subdir || "";

    const files = Array.isArray(body.files) ? body.files : [body];
    try {
      return await writeActionResponse(c, engine, "mobile_workbench.upload", auth, rootId, async () => {
        const results = [];
        for (const file of files) {
          try {
            const contentBase64 = String(file.contentBase64 || "");
            if (!contentBase64) throw routeError("contentBase64 required", "invalid_upload", 400);
            const buffer = Buffer.from(contentBase64, "base64");
            if (buffer.byteLength > MAX_UPLOAD_BYTES) throw routeError("file too large", "file_too_large", 413);
            const target = filesService.writeFileTarget(rootId, subdir, file.name);
            fs.writeFileSync(target.target, buffer);
            results.push({ name: target.filename, ok: true, size: buffer.byteLength });
          } catch (err) {
            results.push({ name: file?.name || null, ok: false, error: err.code || "upload_failed" });
          }
        }
        return {
          ok: results.every((item) => item.ok),
          rootId,
          results,
          files: await filesService.filesForDirectory(rootId, subdir),
        };
      });
    } catch (err) {
      return workbenchError(c, err);
    }
  });

  return route;
}

function readAvatarAvailability(engine) {
  const avatars = {};
  for (const role of ["agent", "user"]) {
    const baseDir = role === "user" ? engine.userDir : engine.agentDir;
    avatars[role] = false;
    if (!baseDir) continue;
    const dir = path.join(baseDir, "avatars");
    try {
      const files = fs.readdirSync(dir);
      avatars[role] = files.some((file) => /\.(png|jpe?g|webp)$/i.test(file));
    } catch {}
  }
  return avatars;
}

function sanitizeAgents(agents) {
  if (!Array.isArray(agents)) return [];
  return agents.map((agent) => ({
    id: agent.id,
    name: agent.name,
    yuan: agent.yuan,
    isPrimary: !!agent.isPrimary,
    isCurrent: !!agent.isCurrent,
    hasAvatar: !!agent.hasAvatar,
    chatModel: agent.chatModel || null,
    homeFolder: agent.homeFolder || null,
    memoryMasterEnabled: agent.memoryMasterEnabled !== false,
  }));
}

function serveContent(c, engine, headOnly) {
  try {
    const auth = authorizeWorkbench(c, engine, "files.read");
    if (auth.response) return auth.response;
    const target = fileService(engine, auth.requestContext)
      .contentTarget(c.req.query("rootId"), c.req.query("subdir") || "", c.req.query("name"));
    const { filePath, filename } = target;
    if (!fs.existsSync(filePath)) return c.json({ error: "file_not_found" }, 404);
    return serveFileContent(c, { filePath, filename, headOnly });
  } catch (err) {
    return workbenchError(c, err);
  }
}

function authorizeWorkbench(c, engine, capability) {
  const requestContext = createRequestContext(c, engine);
  if (requestContext.authPrincipal?.kind === "unknown") return { requestContext, decision: null };
  const decision = requestContext.authorize(capability, {
    kind: "studio",
    studioId: requestContext.studioId,
  });
  if (decision.allowed) return { requestContext, decision };
  recordSecurityAuditEvent(c, engine, {
    action: `mobile_workbench.${capability}`,
    target: { kind: "studio", studioId: requestContext.studioId },
    result: "denied",
    decision,
    errorCode: decision.reason,
  });
  return {
    requestContext,
    decision,
    response: c.json({
      error: "insufficient_scope",
      reason: decision.reason,
      capability,
    }, 403),
  };
}

async function writeActionResponse(c, engine, action, auth, rootId, operation) {
  let lease = null;
  try {
    lease = issueRemoteWriteLease({
      hanakoHome: engine?.hanakoHome,
      requestContext: auth?.requestContext,
      decision: auth?.decision,
      agentId: engine?.currentAgentId || "mobile_workbench",
      sessionId: "mobile_workbench",
      resourceIds: [rootId || "default"],
      mountId: rootId && rootId !== "default" ? rootId : null,
    });
    const result = await operation();
    if (lease) consumeRemoteWriteLease(engine?.hanakoHome, lease);
    return auditActionResult(c, engine, action, result, auth, lease);
  } catch (err) {
    if (lease) revokeRemoteWriteLease(engine?.hanakoHome, lease);
    throw err;
  }
}

function auditActionResult(c, engine, action, result, auth, lease = null) {
  recordSecurityAuditEvent(c, engine, {
    action,
    target: { kind: "studio", studioId: auth?.requestContext?.studioId || null },
    result: result?.ok === false ? "failed" : "success",
    decision: auth?.decision || null,
    leaseId: lease?.leaseId || null,
  });
  return c.json(result);
}

function routeError(message, code, status) {
  const err = new Error(message);
  err.code = code;
  err.status = status;
  return err;
}

function fileService(engine, requestContext) {
  return new MountAwareFileService({
    hanakoHome: engine.hanakoHome,
    defaultRoot: engine.defaultDeskCwd || engine.homeCwd || engine.deskCwd,
    studioId: requestContext?.studioId || engine.getRuntimeContext?.()?.studioId || null,
    createCheckpoint: typeof engine.createUserEditCheckpoint === "function"
      ? (args) => engine.createUserEditCheckpoint(args)
      : null,
  });
}

function workbenchError(c, err) {
  if (err instanceof MountAwareFileError) {
    return c.json({ error: err.code, detail: err.message }, err.status);
  }
  return c.json({ error: err.code || "file_action_failed", detail: err.message }, err.status || 400);
}
