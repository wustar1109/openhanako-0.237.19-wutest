import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HanaEngine } from "../core/engine.js";

describe("HanaEngine.buildTools", () => {
  let tmpDir;

  afterEach(() => {
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    tmpDir = null;
  });

  it("throws when opts.agentDir points at an unknown agent instead of using focus tools", () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-"));
    const focusAgentDir = path.join(tmpDir, "agents", "focus");
    const missingAgentDir = path.join(tmpDir, "agents", "missing");

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.getAgent = vi.fn(() => null);
    engine._pluginManager = null;
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir: focusAgentDir,
        tools: [{ name: "focus_custom_tool", execute: vi.fn() }],
      },
    };

    expect(() => engine.buildTools(tmpDir, undefined, {
      agentDir: missingAgentDir,
      workspace: tmpDir,
    })).toThrow(/agent "missing" not found/);
  });

  it("uses an explicit permission mode provider instead of the desktop session default", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const sessionPath = path.join(tmpDir, "sessions", "bridge.jsonl");
    const execute = vi.fn(async () => ({ details: { executed: true } }));
    const confirmStore = {
      create: vi.fn(() => ({
        confirmId: "confirm-tool-1",
        promise: Promise.resolve({ action: "rejected" }),
      })),
    };

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.getAgent = vi.fn(() => ({ id: "focus", agentDir, tools: [] }));
    engine._pluginManager = null;
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = confirmStore;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "ask");
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir,
        tools: [],
      },
    };

    const { customTools } = engine.buildTools(tmpDir, [
      { name: "stage_files", execute },
    ], {
      agentDir,
      workspace: tmpDir,
      getPermissionMode: () => "operate",
    });

    const result = await customTools[0].execute(
      "call-1",
      { path: "x" },
      { sessionManager: { getSessionFile: () => sessionPath } },
    );

    expect(confirmStore.create).not.toHaveBeenCalled();
    expect(execute).toHaveBeenCalledOnce();
    expect(result.details.executed).toBe(true);
  });

  it("hides stable availability-disabled tools before building the model schema", () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-availability-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const agent = {
      id: "focus",
      agentDir,
      config: { tools: { disabled: ["browser"] } },
      tools: [],
    };

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.getAgent = vi.fn(() => agent);
    engine.isChannelsEnabled = vi.fn(() => false);
    engine._pluginManager = null;
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = {
      agent,
    };

    const { customTools } = engine.buildTools(tmpDir, [
      { name: "browser", execute: vi.fn() },
      { name: "channel", execute: vi.fn() },
      { name: "dm", execute: vi.fn() },
      { name: "cron", execute: vi.fn() },
    ], {
      agentDir,
      workspace: tmpDir,
      getPermissionMode: () => "operate",
    });

    expect(customTools.map((tool) => tool.name)).toEqual(["cron"]);
  });

  it("passes a session workbench execution boundary into plugin tools", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-boundary-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const workspace = path.join(tmpDir, "workspace");
    const sessionPath = path.join(agentDir, "sessions", "main.jsonl");
    const execute = vi.fn(async () => ({ content: [{ type: "text", text: "ok" }] }));
    const agent = {
      id: "focus",
      agentDir,
      config: {},
      tools: [],
    };

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine._runtimeContext = {
      serverId: "server_engine",
      serverNodeId: "node_engine",
      studioId: "studio_engine",
    };
    engine.getAgent = vi.fn(() => agent);
    engine._pluginManager = {
      getAllTools: () => [{
        name: "plugin_tool",
        execute,
      }],
    };
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = { agent };

    const { customTools } = engine.buildTools(workspace, [], {
      agentDir,
      workspace,
      getSessionPath: () => sessionPath,
      getPermissionMode: () => "operate",
    });
    const pluginTool = customTools.find((tool) => tool.name === "plugin_tool");

    await pluginTool.execute("call-1", { ok: true }, {
      sessionManager: { getSessionFile: () => sessionPath },
    });

    expect(execute).toHaveBeenCalledWith(
      "call-1",
      { ok: true },
      expect.objectContaining({
        sessionManager: expect.any(Object),
      }),
      undefined,
      expect.objectContaining({
        agentId: "focus",
        serverNodeId: "node_engine",
        sessionPath,
        executionBoundary: expect.objectContaining({
          boundaryId: "execb_node_engine_studio_engine",
          serverNodeId: "node_engine",
          studioId: "studio_engine",
          workbench: {
            kind: "legacy_agent_workbench",
            root: workspace,
          },
        }),
      }),
    );
  });

  it("passes the explicit buildTools session path into plugin tool runtime context", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-plugin-session-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const workspace = path.join(tmpDir, "workspace");
    const bridgeSessionPath = path.join(agentDir, "sessions", "bridge", "owner", "chat.jsonl");
    const execute = vi.fn(async () => ({ content: [{ type: "text", text: "ok" }] }));
    const agent = {
      id: "focus",
      agentDir,
      config: {},
      tools: [],
    };

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine._runtimeContext = {
      serverId: "server_engine",
      serverNodeId: "node_engine",
      studioId: "studio_engine",
    };
    engine.getAgent = vi.fn(() => agent);
    engine._pluginManager = {
      getAllTools: () => [{
        name: "plugin_tool",
        execute,
      }],
    };
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = { agent };

    const { customTools } = engine.buildTools(workspace, [], {
      agentDir,
      workspace,
      getSessionPath: () => bridgeSessionPath,
      getPermissionMode: () => "operate",
    });
    const pluginTool = customTools.find((tool) => tool.name === "plugin_tool");

    await pluginTool.execute("call-1", { ok: true }, {});

    expect(execute).toHaveBeenCalledWith(
      "call-1",
      { ok: true },
      {},
      undefined,
      expect.objectContaining({
        sessionPath: bridgeSessionPath,
      }),
    );
  });

  it("passes Pi SDK fifth-argument session ctx into plugin tools", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-plugin-pi-ctx-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const workspace = path.join(tmpDir, "workspace");
    const desktopSessionPath = path.join(agentDir, "sessions", "desktop.jsonl");
    const execute = vi.fn(async () => ({ content: [{ type: "text", text: "ok" }] }));
    const agent = {
      id: "focus",
      agentDir,
      config: {},
      tools: [],
    };

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine._runtimeContext = {
      serverId: "server_engine",
      serverNodeId: "node_engine",
      studioId: "studio_engine",
    };
    engine.getAgent = vi.fn(() => agent);
    engine._pluginManager = {
      getAllTools: () => [{
        name: "plugin_tool",
        execute,
      }],
    };
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = { agent };

    const { customTools } = engine.buildTools(workspace, [], {
      agentDir,
      workspace,
      getPermissionMode: () => "operate",
    });
    const pluginTool = customTools.find((tool) => tool.name === "plugin_tool");
    const signal = new AbortController().signal;
    const onUpdate = vi.fn();

    await pluginTool.execute("call-1", { ok: true }, signal, onUpdate, {
      sessionManager: { getSessionFile: () => desktopSessionPath },
    });

    expect(execute).toHaveBeenCalledWith(
      "call-1",
      { ok: true },
      signal,
      onUpdate,
      expect.objectContaining({
        sessionPath: desktopSessionPath,
      }),
    );
  });

  it("registers files created or modified by write and edit tools in the active session", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-touch-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const workspace = path.join(tmpDir, "workspace");
    const sessionPath = path.join(agentDir, "sessions", "touch.jsonl");
    fs.mkdirSync(path.dirname(sessionPath), { recursive: true });
    fs.mkdirSync(workspace, { recursive: true });
    fs.writeFileSync(sessionPath, "{}\n");

    const registerSessionFile = vi.fn(({ sessionPath, filePath, label, origin, operation }) => ({
      id: `sf_${operation}`,
      sessionPath,
      filePath,
      label,
      origin,
      operation,
    }));
    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.registerSessionFile = registerSessionFile;
    engine.getAgent = vi.fn(() => ({ id: "focus", agentDir, tools: [] }));
    engine._pluginManager = null;
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: false });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir,
        tools: [],
      },
    };

    const { tools } = engine.buildTools(workspace, [], {
      agentDir,
      workspace,
      getSessionPath: () => sessionPath,
    });
    const write = tools.find(tool => tool.name === "write");
    const edit = tools.find(tool => tool.name === "edit");

    const writeResult = await write.execute("write-1", { path: "draft.md", content: "hello\n" });
    const editResult = await edit.execute("edit-1", {
      path: "draft.md",
      edits: [{ oldText: "hello", newText: "hello Hana" }],
    });

    expect(registerSessionFile).toHaveBeenCalledWith(expect.objectContaining({
      sessionPath,
      filePath: path.join(workspace, "draft.md"),
      label: "draft.md",
      origin: "agent_write",
      operation: "created",
    }));
    expect(registerSessionFile).toHaveBeenCalledWith(expect.objectContaining({
      sessionPath,
      filePath: path.join(workspace, "draft.md"),
      label: "draft.md",
      origin: "agent_edit",
      operation: "modified",
    }));
    expect(writeResult.details.sessionFile).toMatchObject({
      id: "sf_created",
      filePath: path.join(workspace, "draft.md"),
      origin: "agent_write",
    });
    expect(editResult.details.sessionFile).toMatchObject({
      id: "sf_modified",
      filePath: path.join(workspace, "draft.md"),
      origin: "agent_edit",
    });
  });

  it("blocks direct agent config edits from built-in file tools even when sandbox is disabled", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-managed-config-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const workspace = path.join(tmpDir, "workspace");
    const configPath = path.join(agentDir, "config.yaml");
    fs.mkdirSync(agentDir, { recursive: true });
    fs.mkdirSync(workspace, { recursive: true });
    fs.writeFileSync(configPath, "agent:\n  name: Hana\n  yuan: hanako\n", "utf-8");

    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.registerSessionFile = vi.fn();
    engine.getAgent = vi.fn(() => ({ id: "focus", agentDir, tools: [] }));
    engine._pluginManager = null;
    engine._prefs = { getFileBackup: () => ({ enabled: false }) };
    engine._readPreferences = () => ({ sandbox: false });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir,
        tools: [],
      },
    };

    const { tools } = engine.buildTools(workspace, [], {
      agentDir,
      workspace,
    });
    const write = tools.find(tool => tool.name === "write");
    const edit = tools.find(tool => tool.name === "edit");

    const writeResult = await write.execute("write-config", {
      path: configPath,
      content: "agent:\n  name: Hana\n  yuan: caikangyong\n",
    });
    const editResult = await edit.execute("edit-config", {
      path: configPath,
      edits: [{ oldText: "yuan: hanako", newText: "yuan: caikangyong" }],
    });

    expect(writeResult.content[0].text).toContain("managed");
    expect(editResult.content[0].text).toContain("managed");
    expect(fs.readFileSync(configPath, "utf-8")).toContain("yuan: hanako");
    expect(engine.registerSessionFile).not.toHaveBeenCalled();
  });

  it("keeps plugin dev Agent tools hidden until the global dev setting is enabled", () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-dev-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.getAgent = vi.fn(() => ({ id: "focus", agentDir, tools: [] }));
    engine._pluginManager = null;
    engine._pluginDevService = { getDiagnostics: vi.fn() };
    engine._prefs = {
      getFileBackup: () => ({ enabled: false }),
      getPluginDevToolsEnabled: () => false,
    };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir,
        tools: [],
      },
    };

    const { customTools } = engine.buildTools(tmpDir, [], {
      agentDir,
      workspace: tmpDir,
      getPermissionMode: () => "operate",
    });

    expect(customTools.some((tool) => tool.name.startsWith("plugin_dev_"))).toBe(false);
  });

  it("adds plugin dev Agent tools when the user enables the dev setting", () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-tools-dev-"));
    const agentDir = path.join(tmpDir, "agents", "focus");
    const engine = Object.create(HanaEngine.prototype);
    engine.hanakoHome = tmpDir;
    engine.getAgent = vi.fn(() => ({ id: "focus", agentDir, tools: [] }));
    engine._pluginManager = null;
    engine._pluginDevService = {
      installFromSource: vi.fn(),
      reloadPlugin: vi.fn(),
      enablePlugin: vi.fn(),
      disablePlugin: vi.fn(),
      resetPlugin: vi.fn(),
      uninstallPlugin: vi.fn(),
      invokeTool: vi.fn(),
      getDiagnostics: vi.fn(),
      listSurfaces: vi.fn(),
      describeSurfaceDebug: vi.fn(),
      runScenario: vi.fn(),
    };
    engine._prefs = {
      getFileBackup: () => ({ enabled: false }),
      getPluginDevToolsEnabled: () => true,
    };
    engine._readPreferences = () => ({ sandbox: true });
    engine._confirmStore = null;
    engine._emitEvent = vi.fn();
    engine.getSessionPermissionMode = vi.fn(() => "operate");
    engine._agentMgr = {
      agent: {
        id: "focus",
        agentDir,
        tools: [],
      },
    };

    const { customTools } = engine.buildTools(tmpDir, [], {
      agentDir,
      workspace: tmpDir,
      getPermissionMode: () => "operate",
    });

    expect(customTools.map((tool) => tool.name)).toEqual(expect.arrayContaining([
      "plugin_dev_install",
      "plugin_dev_reload",
      "plugin_dev_uninstall",
      "plugin_dev_invoke_tool",
      "plugin_dev_run_scenario",
    ]));
  });
});
