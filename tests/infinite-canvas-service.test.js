import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, describe, expect, it } from "vitest";
import {
  createInfiniteCanvasEnv,
  resolvePythonCandidates,
} from "../server/infinite-canvas/service.js";

const tempDirs = [];

function tempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "openhanako-ic-"));
  tempDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe("Infinite-Canvas service helpers", () => {
  it("prefers a repository-local virtualenv Python on Windows", () => {
    const repoDir = tempDir();
    const python = path.join(repoDir, ".venv", "Scripts", "python.exe");
    fs.mkdirSync(path.dirname(python), { recursive: true });
    fs.writeFileSync(python, "");

    expect(resolvePythonCandidates(repoDir, "win32")[0]).toBe(python);
  });

  it("builds OpenHanako-owned runtime directories", () => {
    const repoDir = "D:\\repo\\third_party\\Infinite-Canvas";
    const hanakoHome = "D:\\hana-home";
    const { home, env } = createInfiniteCanvasEnv({ repoDir, hanakoHome });

    expect(home).toBe(path.join(hanakoHome, "infinite-canvas"));
    expect(env.INFINITE_CANVAS_DATA_DIR).toBe(path.join(home, "data"));
    expect(env.INFINITE_CANVAS_OUTPUT_DIR).toBe(path.join(home, "output"));
    expect(env.INFINITE_CANVAS_ASSETS_DIR).toBe(path.join(home, "assets"));
    expect(env.INFINITE_CANVAS_WORKFLOW_DIR).toBe(path.join(home, "workflows"));
    expect(env.INFINITE_CANVAS_API_DIR).toBe(path.join(home, "API"));
    expect(env.INFINITE_CANVAS_STATIC_DIR).toBe(path.join(repoDir, "static"));
    expect(env.PYTHONUTF8).toBe("1");
  });
});
