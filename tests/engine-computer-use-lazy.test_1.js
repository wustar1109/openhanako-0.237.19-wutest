import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HanaEngine } from "../core/engine.js";

describe("HanaEngine Computer Use lazy runtime", () => {
  let tmpDir = null;

  afterEach(() => {
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    tmpDir = null;
  });

  function createEngine() {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-engine-computer-use-"));
    return new HanaEngine({
      hanakoHome: tmpDir,
      productDir: tmpDir,
      agentId: "hana",
    });
  }

  it("does not construct the Computer Use runtime during engine construction", () => {
    const engine = createEngine();

    expect(engine._computerProviders).toBeNull();
    expect(engine._computerHost).toBeNull();
  });

  it("constructs the Computer Use runtime when the global switch is enabled", () => {
    const engine = createEngine();

    const disabled = engine.setComputerUseSettings({ enabled: false });
    expect(disabled.enabled).toBe(false);
    expect(engine._computerProviders).toBeNull();
    expect(engine._computerHost).toBeNull();

    const enabled = engine.setComputerUseSettings({ enabled: true });
    expect(enabled.enabled).toBe(true);
    expect(engine._computerProviders).toBeTruthy();
    expect(engine._computerHost).toBeTruthy();
  });

  it("disposes the lazy Computer Use runtime during engine shutdown", async () => {
    const engine = createEngine();
    engine.setComputerUseSettings({ enabled: true });
    const dispose = vi.fn(async () => {});
    engine._computerHost = { dispose };

    await engine.dispose();

    expect(dispose).toHaveBeenCalledOnce();
    expect(engine._computerHost).toBeNull();
    expect(engine._computerProviders).toBeNull();
  });
});
