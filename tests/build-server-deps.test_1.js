import fs from "fs";
import os from "os";
import path from "path";
import { execFileSync } from "child_process";
import { afterEach, describe, expect, it } from "vitest";
import {
  buildExternalPackage,
  buildJiebaRuntimeSmokeScript,
  collectInstalledOptionalDependencyDirs,
  verifyExternalEntrypoints,
} from "../scripts/build-server-deps.mjs";

const tempDirs = [];

function makeTempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "hana-build-server-deps-"));
  tempDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe("build-server external dependency packaging", () => {
  it("pins server externals and selected runtime transitives to the root lock versions", () => {
    const rootPkg = {
      name: "hanako",
      version: "1.0.1",
    };
    const rootLock = {
      name: "hanako",
      version: "1.0.0",
      lockfileVersion: 3,
      requires: true,
      packages: {
        "": {
          name: "hanako",
          version: "1.0.0",
          dependencies: {
            jsdom: "^29.0.2",
            vite: "^7.0.0",
          },
          devDependencies: {
            vite: "^7.0.0",
          },
        },
        "node_modules/jsdom": {
          version: "29.0.2",
          dependencies: {
            "lru-cache": "^11.2.7",
          },
        },
        "node_modules/lru-cache": {
          version: "11.2.7",
        },
        "node_modules/vite": {
          version: "7.3.0",
          dev: true,
        },
      },
    };

    const serverPkg = buildExternalPackage(
      rootPkg,
      {
        jsdom: "^29.0.2",
      },
      {
        rootLock,
        pinnedTransitiveDeps: ["lru-cache"],
      },
    );

    expect(serverPkg).toEqual({
      name: "hanako-server",
      version: "1.0.1",
      type: "module",
      dependencies: {
        jsdom: "29.0.2",
        "lru-cache": "11.2.7",
      },
    });
  });

  it("protects installed optional runtime packages owned by server externals", () => {
    const outDir = makeTempDir();
    const nmDir = path.join(outDir, "node_modules");
    const rootPackageDir = path.join(nmDir, "@node-rs", "jieba");
    const nativePackageDir = path.join(nmDir, "@node-rs", "jieba-darwin-arm64");
    fs.mkdirSync(rootPackageDir, { recursive: true });
    fs.mkdirSync(nativePackageDir, { recursive: true });
    fs.writeFileSync(path.join(rootPackageDir, "package.json"), JSON.stringify({
      name: "@node-rs/jieba",
      optionalDependencies: {
        "@node-rs/jieba-darwin-arm64": "2.0.1",
        "@node-rs/jieba-linux-x64-gnu": "2.0.1",
      },
    }));

    const dirs = collectInstalledOptionalDependencyDirs(nmDir, ["@node-rs/jieba"]);

    expect(dirs).toEqual([nativePackageDir]);
  });

  it("generates a runtime smoke script that requires jieba, dict, and custom dictionary terms", () => {
    const outDir = makeTempDir();
    const rootPackageDir = path.join(outDir, "node_modules", "@node-rs", "jieba");
    fs.mkdirSync(rootPackageDir, { recursive: true });
    fs.writeFileSync(path.join(outDir, "package.json"), JSON.stringify({ type: "module" }));
    fs.writeFileSync(path.join(rootPackageDir, "dict.js"), "module.exports.dict = Buffer.from('dict')\n");
    fs.writeFileSync(path.join(rootPackageDir, "index.js"), [
      "class Jieba {",
      "  static withDict(dict) { if (!Buffer.isBuffer(dict)) throw new Error('missing dict'); return new Jieba(); }",
      "  loadDict(dict) { this.customDict = dict.toString('utf8'); }",
      "  cutForSearch() {",
      "    if (!this.customDict.includes('session_search')) throw new Error('missing custom dict');",
      "    return ['聊天记录', 'A2A通信', 'session_search'];",
      "  }",
      "}",
      "module.exports = { Jieba };",
    ].join("\n"));

    const scriptPath = path.join(outDir, ".jieba-smoke.mjs");
    fs.writeFileSync(scriptPath, buildJiebaRuntimeSmokeScript());

    expect(() => execFileSync(process.execPath, [scriptPath], { cwd: outDir }))
      .not.toThrow();
  });

  it("fails fast when an installed external package export resolves to a missing file", () => {
    const outDir = makeTempDir();
    const packageDir = path.join(outDir, "node_modules", "bad-export-package");
    fs.mkdirSync(path.join(packageDir, "dist", "commonjs"), { recursive: true });
    fs.writeFileSync(path.join(outDir, "package.json"), JSON.stringify({ type: "module" }));
    fs.writeFileSync(path.join(packageDir, "dist", "commonjs", "index.min.js"), "module.exports = {};\n");
    fs.writeFileSync(path.join(packageDir, "package.json"), JSON.stringify({
      name: "bad-export-package",
      version: "1.0.0",
      exports: {
        ".": {
          require: {
            node: {
              default: "./dist/commonjs/node/index.min.js",
            },
            default: "./dist/commonjs/index.min.js",
          },
        },
      },
    }));

    expect(() => verifyExternalEntrypoints(outDir, ["bad-export-package"])).toThrow(
      /bad-export-package.*dist\/commonjs\/node\/index\.min\.js/s,
    );
  });

  it("accepts import-only package exports when the runtime target exists", () => {
    const outDir = makeTempDir();
    const packageDir = path.join(outDir, "node_modules", "esm-only-package");
    fs.mkdirSync(path.join(packageDir, "dist"), { recursive: true });
    fs.writeFileSync(path.join(packageDir, "dist", "index.js"), "export const ok = true;\n");
    fs.writeFileSync(path.join(packageDir, "package.json"), JSON.stringify({
      name: "esm-only-package",
      version: "1.0.0",
      type: "module",
      exports: {
        ".": {
          types: "./dist/index.d.ts",
          import: "./dist/index.js",
        },
      },
    }));

    expect(() => verifyExternalEntrypoints(outDir, ["esm-only-package"])).not.toThrow();
  });
});
