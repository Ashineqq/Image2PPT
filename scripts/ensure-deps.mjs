#!/usr/bin/env node
// Pre-start gate for `pnpm start`: auto-install missing deps, then boot.
// Runs as the `prestart` hook, so `pnpm start` stays a single command.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PY = process.env.PYTHON ?? "python3";
const REQ = join(ROOT, "requirements.txt");

function run(cmd, args) {
  execFileSync(cmd, args, { cwd: ROOT, stdio: "inherit" });
}

function runOk(cmd, args) {
  try {
    execFileSync(cmd, args, { cwd: ROOT, stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

// ---- frontend: pnpm install when node_modules is missing ----
const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
const needsNode =
  Object.keys(pkg.dependencies ?? {}).length > 0 ||
  Object.keys(pkg.devDependencies ?? {}).length > 0;
if (needsNode && !existsSync(join(ROOT, "node_modules"))) {
  console.log("[start] node_modules 缺失，正在 pnpm install…");
  run("pnpm", ["install"]);
} else {
  console.log("[start] 前端依赖 ok");
}

// ---- backend: probe imports, pip install what is missing ----
const IMPORTS = {
  "python-pptx": "pptx",
  pillow: "PIL",
  numpy: "numpy",
  "opencv-python": "cv2",
  onnxruntime: "onnxruntime",
  huggingface_hub: "huggingface_hub",
  pymupdf: "pymupdf",
  requests: "requests",
};

function missingModules() {
  const names = [...new Set(Object.values(IMPORTS))];
  if (runOk(PY, ["-c", names.map((m) => `import ${m}`).join(";")])) return [];
  return names.filter((m) => !runOk(PY, ["-c", `import ${m}`]));
}

const missing = missingModules();
if (missing.length === 0) {
  console.log("[start] 后端依赖 ok");
} else {
  console.log(`[start] 缺少 Python 包（${missing.join(", ")}），正在安装…`);
  let installed = runOk(PY, ["-m", "pip", "install", "-r", REQ]);
  if (!installed) {
    console.log("[start] 常规安装失败，改用 --break-system-packages 重试…");
    installed = runOk(PY, [
      "-m", "pip", "install", "--break-system-packages", "-r", REQ,
    ]);
  }
  if (!installed) {
    console.log("[start] 镜像源无匹配版本，改用官方 PyPI 重试…");
    installed = runOk(PY, [
      "-m", "pip", "install", "--break-system-packages",
      "-i", "https://pypi.org/simple", "-r", REQ,
    ]);
  }
  const still = installed ? missingModules() : missing;
  if (!installed || still.length > 0) {
    console.error(
      `[start] 依赖安装失败（仍缺：${still.join(", ")}），` +
        `请手动执行：${PY} -m pip install -r requirements.txt`,
    );
    process.exit(1);
  }
  console.log("[start] 后端依赖安装完成");
}
