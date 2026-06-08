import fs from "fs";
import fsp from "fs/promises";
import net from "net";
import path from "path";
import { spawn } from "child_process";

const LOOPBACK_HOST = "127.0.0.1";
const HEALTH_PATH = "/api/config";
const START_TIMEOUT_MS = 20_000;
const HEALTH_INTERVAL_MS = 350;

let serviceState = {
  process: null,
  port: null,
  url: null,
  startPromise: null,
  lastOptions: null,
  error: null,
};

function writeLog(log, level, message) {
  const target = level === "error" ? log?.error : level === "warn" ? log?.warn : log?.log;
  if (typeof target === "function") target.call(log, message);
  else console[level === "error" ? "error" : level === "warn" ? "warn" : "log"](message);
}

function dependencyHint(repoDir) {
  return `Infinite-Canvas Python dependencies are missing. Run: cd ${repoDir} && python -m venv .venv && pip install -r requirements.txt`;
}

function isRunning(child) {
  return !!child && child.exitCode === null && child.signalCode === null;
}

export function getInfiniteCanvasServiceUrl() {
  if (!isRunning(serviceState.process)) return null;
  return serviceState.url;
}

export function getInfiniteCanvasServiceError() {
  return serviceState.error;
}

export function getInfiniteCanvasRepoRoot(repoRoot) {
  return path.join(repoRoot, "third_party", "Infinite-Canvas");
}

export function resolvePythonCandidates(repoDir, platform = process.platform) {
  const candidates = [];
  const venvPython = platform === "win32"
    ? path.join(repoDir, ".venv", "Scripts", "python.exe")
    : path.join(repoDir, ".venv", "bin", "python");
  if (fs.existsSync(venvPython)) candidates.push(venvPython);
  if (platform === "win32") candidates.push("python", "py", "python3");
  else candidates.push("python3", "python");
  return [...new Set(candidates)];
}

async function allocatePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, LOOPBACK_HOST, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((err) => {
        if (err) reject(err);
        else if (port) resolve(port);
        else reject(new Error("failed to allocate port"));
      });
    });
  });
}

async function copyMissingTree(srcDir, destDir) {
  if (!fs.existsSync(srcDir)) return;
  await fsp.mkdir(destDir, { recursive: true });
  for (const entry of await fsp.readdir(srcDir, { withFileTypes: true })) {
    const src = path.join(srcDir, entry.name);
    const dest = path.join(destDir, entry.name);
    if (entry.isDirectory()) {
      await copyMissingTree(src, dest);
    } else if (!fs.existsSync(dest)) {
      await fsp.copyFile(src, dest);
    }
  }
}

export async function seedInfiniteCanvasRuntime({ repoDir, infiniteCanvasHome }) {
  const workflowDir = path.join(infiniteCanvasHome, "workflows");
  await fsp.mkdir(infiniteCanvasHome, { recursive: true });
  await fsp.mkdir(path.join(infiniteCanvasHome, "data"), { recursive: true });
  await fsp.mkdir(path.join(infiniteCanvasHome, "output"), { recursive: true });
  await fsp.mkdir(path.join(infiniteCanvasHome, "assets"), { recursive: true });
  await fsp.mkdir(path.join(infiniteCanvasHome, "API"), { recursive: true });
  await copyMissingTree(path.join(repoDir, "workflows"), workflowDir);
}

export function createInfiniteCanvasEnv({ repoDir, hanakoHome }) {
  const infiniteCanvasHome = path.join(hanakoHome, "infinite-canvas");
  return {
    home: infiniteCanvasHome,
    env: {
      ...process.env,
      INFINITE_CANVAS_HOME: infiniteCanvasHome,
      INFINITE_CANVAS_DATA_DIR: path.join(infiniteCanvasHome, "data"),
      INFINITE_CANVAS_OUTPUT_DIR: path.join(infiniteCanvasHome, "output"),
      INFINITE_CANVAS_ASSETS_DIR: path.join(infiniteCanvasHome, "assets"),
      INFINITE_CANVAS_WORKFLOW_DIR: path.join(infiniteCanvasHome, "workflows"),
      INFINITE_CANVAS_API_DIR: path.join(infiniteCanvasHome, "API"),
      INFINITE_CANVAS_STATIC_DIR: path.join(repoDir, "static"),
      PYTHONUTF8: "1",
    },
  };
}

async function spawnPython(candidate, args, opts) {
  return new Promise((resolve, reject) => {
    const child = spawn(candidate, args, opts);
    child.once("spawn", () => resolve(child));
    child.once("error", reject);
  });
}

async function waitForHealthy(url, child) {
  const deadline = Date.now() + START_TIMEOUT_MS;
  let lastError = null;
  while (Date.now() < deadline) {
    if (!isRunning(child)) {
      throw new Error(`Infinite-Canvas exited before becoming ready (code=${child.exitCode}, signal=${child.signalCode})`);
    }
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 1000);
      const res = await fetch(`${url}${HEALTH_PATH}`, { signal: controller.signal });
      clearTimeout(timer);
      if (res.ok) return true;
      lastError = new Error(`health returned ${res.status}`);
    } catch (err) {
      lastError = err;
    }
    await new Promise(resolve => setTimeout(resolve, HEALTH_INTERVAL_MS));
  }
  throw lastError || new Error("Infinite-Canvas health check timed out");
}

export async function startInfiniteCanvasService(options = serviceState.lastOptions) {
  if (getInfiniteCanvasServiceUrl()) return serviceState.url;
  if (serviceState.startPromise) return serviceState.startPromise;
  if (!options?.repoRoot || !options?.hanakoHome) {
    serviceState.error = "Infinite-Canvas service options are missing";
    return null;
  }

  serviceState.lastOptions = options;
  const { repoRoot, hanakoHome, log } = options;
  const repoDir = getInfiniteCanvasRepoRoot(repoRoot);

  serviceState.startPromise = (async () => {
    try {
      serviceState.error = null;
      if (!fs.existsSync(path.join(repoDir, "main.py"))) {
        throw new Error(`Infinite-Canvas source not found at ${repoDir}`);
      }

      const { home, env } = createInfiniteCanvasEnv({ repoDir, hanakoHome });
      await seedInfiniteCanvasRuntime({ repoDir, infiniteCanvasHome: home });
      const port = await allocatePort();
      const url = `http://${LOOPBACK_HOST}:${port}`;
      const args = ["-m", "uvicorn", "main:app", "--host", LOOPBACK_HOST, "--port", String(port)];
      const candidates = resolvePythonCandidates(repoDir);
      let lastError = null;

      for (const python of candidates) {
        let child = null;
        try {
          child = await spawnPython(python, args, {
            cwd: repoDir,
            env,
            stdio: ["ignore", "pipe", "pipe"],
            windowsHide: true,
          });
          child.stdout?.on("data", chunk => writeLog(log, "log", `[infinite-canvas] ${String(chunk).trimEnd()}`));
          child.stderr?.on("data", chunk => writeLog(log, "warn", `[infinite-canvas] ${String(chunk).trimEnd()}`));
          child.once("exit", (code, signal) => {
            if (serviceState.process === child) {
              writeLog(log, code === 0 ? "log" : "warn", `[infinite-canvas] exited code=${code} signal=${signal || ""}`);
              serviceState.process = null;
              serviceState.port = null;
              serviceState.url = null;
            }
          });

          serviceState.process = child;
          serviceState.port = port;
          serviceState.url = url;
          await waitForHealthy(url, child);
          writeLog(log, "log", `[infinite-canvas] ready at ${url}`);
          return url;
        } catch (err) {
          lastError = err;
          if (child && isRunning(child)) child.kill();
          if (serviceState.process === child) {
            serviceState.process = null;
            serviceState.port = null;
            serviceState.url = null;
          }
          writeLog(log, "warn", `[infinite-canvas] failed with ${python}: ${err.message}`);
        }
      }

      throw new Error(`${lastError?.message || "no Python runtime available"}\n${dependencyHint(repoDir)}`);
    } catch (err) {
      serviceState.error = err instanceof Error ? err.message : String(err);
      writeLog(log, "error", `[infinite-canvas] startup failed: ${serviceState.error}`);
      return null;
    } finally {
      serviceState.startPromise = null;
    }
  })();

  return serviceState.startPromise;
}

export async function stopInfiniteCanvasService() {
  const child = serviceState.process;
  serviceState.process = null;
  serviceState.port = null;
  serviceState.url = null;
  if (!child || !isRunning(child)) return;
  child.kill();
  await new Promise(resolve => setTimeout(resolve, 500));
  if (isRunning(child)) {
    try { child.kill("SIGKILL"); } catch {}
  }
}
