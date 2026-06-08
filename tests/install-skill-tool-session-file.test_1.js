import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createInstallSkillTool } from "../lib/tools/install-skill.js";

describe("install_skill global skill-pool installation", () => {
  let tmpDir = null;

  afterEach(() => {
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    tmpDir = null;
  });

  it("installs skill_content into the global skill pool and enables the current agent through the callback", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-install-skill-tool-"));
    const agentDir = path.join(tmpDir, "agent");
    const userSkillsDir = path.join(tmpDir, "user-skills");
    fs.mkdirSync(agentDir, { recursive: true });
    const sessionPath = "/sessions/install-tool.jsonl";
    const registerSessionFile = vi.fn(({ sessionPath, filePath, label, origin, storageKind }) => ({
      id: "sf_installed_skill",
      sessionPath,
      filePath,
      realPath: filePath,
      displayName: label,
      filename: label,
      label,
      ext: "md",
      mime: "text/markdown",
      size: 32,
      kind: "markdown",
      origin,
      storageKind,
      createdAt: 1,
    }));
    const onInstalled = vi.fn();
    const tool = createInstallSkillTool({
      agentDir,
      getUserSkillsDir: () => userSkillsDir,
      getConfig: () => ({
        capabilities: {
          learn_skills: {
            enabled: true,
            safety_review: false,
          },
        },
      }),
      resolveUtilityConfig: () => null,
      onInstalled,
      registerSessionFile,
    });

    const result = await tool.execute("call-1", {
      skill_name: "demo-skill",
      skill_content: "---\nname: demo-skill\n---\n# Demo\n",
      reason: "test",
    }, null, null, {
      sessionManager: { getSessionFile: () => sessionPath },
    });

    const skillFilePath = path.join(userSkillsDir, "demo-skill", "SKILL.md");
    expect(registerSessionFile).toHaveBeenCalledWith({
      sessionPath,
      filePath: skillFilePath,
      label: "SKILL.md",
      origin: "install_skill_output",
      storageKind: "install_output",
    });
    expect(result.details).toMatchObject({
      skillName: "demo-skill",
      skillFilePath,
      installedSkillSource: {
        kind: "skill_source",
        owner: "user",
        skillName: "demo-skill",
        filePath: skillFilePath,
        baseDir: path.dirname(skillFilePath),
        editable: true,
        readonly: false,
      },
      installedFile: {
        id: "sf_installed_skill",
        fileId: "sf_installed_skill",
        sessionPath,
        filePath: skillFilePath,
        origin: "install_skill_output",
        storageKind: "install_output",
      },
    });
    expect(fs.readFileSync(skillFilePath, "utf-8")).toContain("default-enabled: false");
    expect(onInstalled).toHaveBeenCalledWith("demo-skill");
  });

  it("does not overwrite an existing global skill with different content", async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-install-skill-tool-"));
    const agentDir = path.join(tmpDir, "agents", "agent-b");
    const userSkillsDir = path.join(tmpDir, "user-skills");
    fs.mkdirSync(path.join(userSkillsDir, "demo-skill"), { recursive: true });
    fs.mkdirSync(agentDir, { recursive: true });
    const existingContent = "---\nname: demo-skill\n---\n# Existing\n";
    fs.writeFileSync(path.join(userSkillsDir, "demo-skill", "SKILL.md"), existingContent, "utf-8");
    const onInstalled = vi.fn();
    const tool = createInstallSkillTool({
      agentDir,
      getUserSkillsDir: () => userSkillsDir,
      getConfig: () => ({
        capabilities: {
          learn_skills: {
            enabled: true,
            safety_review: false,
          },
        },
      }),
      resolveUtilityConfig: () => null,
      onInstalled,
      registerSessionFile: vi.fn(),
    });

    const result = await tool.execute("call-1", {
      skill_name: "demo-skill",
      skill_content: "---\nname: demo-skill\n---\n# Different\n",
      reason: "test",
    }, null, null, {});

    const originalPath = path.join(userSkillsDir, "demo-skill", "SKILL.md");
    const migratedPath = path.join(userSkillsDir, "demo-skill-agent-b", "SKILL.md");
    expect(fs.readFileSync(originalPath, "utf-8")).toBe(existingContent);
    expect(fs.readFileSync(migratedPath, "utf-8")).toContain("name: demo-skill-agent-b");
    expect(result.details.skillName).toBe("demo-skill-agent-b");
    expect(onInstalled).toHaveBeenCalledWith("demo-skill-agent-b");
  });
});
