import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createHeartbeat } from "../lib/desk/heartbeat.js";

let tempRoot;

describe("heartbeat workspace output directories", () => {
  beforeEach(() => {
    tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "hana-heartbeat-output-"));
  });

  afterEach(() => {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  });

  it("tells the agent to use visible OH-Works patrol and activity folders", async () => {
    const onBeat = vi.fn().mockResolvedValue(undefined);
    const heartbeat = createHeartbeat({
      getDeskFiles: async () => [],
      getWorkspacePath: () => tempRoot,
      getAgentName: () => "小/花:*?",
      registryPath: path.join(tempRoot, ".registry", "jian-registry.json"),
      onBeat,
      intervalMinutes: 31,
      locale: "zh-CN",
    });

    await heartbeat.beat();

    expect(onBeat).toHaveBeenCalledOnce();
    const prompt = onBeat.mock.calls[0][0];
    expect(prompt).toContain("OH-Works/小花的巡检/patrol-log.md");
    expect(prompt).toContain("OH-Works/小花-activity/");
    expect(prompt).not.toContain("HeartBeat");
  });

  it("gives jian patrols a status tool that writes a program-owned snapshot", async () => {
    const jianPath = path.join(tempRoot, "jian.md");
    const instructions = "帮我巡检这个目录，执行五次。";
    fs.writeFileSync(jianPath, `${instructions}\n`, "utf-8");

    const onBeat = vi.fn().mockResolvedValue(undefined);
    const onJianBeat = vi.fn(async (_prompt, _cwd, opts) => {
      const statusTool = opts.customTools.find((tool) => tool.name === "jian_update_status");
      await statusTool.execute("tool-call-1", {
        status: "in_progress",
        progress: "4/5",
        note: "已完成第 4 次巡检，下次继续第 5 次。",
      });
    });
    const heartbeat = createHeartbeat({
      getDeskFiles: async () => [],
      getWorkspacePath: () => tempRoot,
      getAgentName: () => "Hana",
      registryPath: path.join(tempRoot, ".registry", "jian-registry.json"),
      onBeat,
      onJianBeat,
      intervalMinutes: 31,
      locale: "zh-CN",
    });

    await heartbeat.beat();

    expect(onJianBeat).toHaveBeenCalledOnce();
    const [prompt, cwd, opts] = onJianBeat.mock.calls[0];
    expect(cwd).toBe(tempRoot);
    expect(prompt).toContain("上次任务快照");
    expect(prompt).toContain("不要直接编辑 jian.md");
    expect(prompt).not.toContain("追加到 jian.md");
    expect(opts.customTools.map((tool) => tool.name)).toContain("jian_update_status");

    const next = fs.readFileSync(jianPath, "utf-8");
    expect(next).toContain(instructions);
    expect(next).toContain("上次任务快照：");
    expect(next).toContain(instructions);
    expect(next).toContain("执行状态：");
    expect(next).toContain("- 状态：进行中");
    expect(next).toContain("- 进度：4/5");
    expect(next).toContain("- 说明：已完成第 4 次巡检，下次继续第 5 次。");
  });
});
