import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return fs.readFileSync(path.join(frontendDir, relativePath), "utf8");
}

test("settings use a desktop rail and a compact mobile category selector", () => {
  const page = read("app/config/page.tsx");
  const icons = read("components/SettingsSectionIcon.tsx");

  for (const label of ["文献与存储", "模型与翻译", "工具与解析", "高级"]) {
    assert.match(page, new RegExp(label));
  }
  assert.match(page, /aria-label="设置分类"/);
  assert.match(page, /aria-pressed=\{activeSection === section\.id\}/);
  assert.match(page, /min-\[560px\]:grid-cols-\[10\.5rem_minmax\(0,1fr\)\]/);
  assert.match(page, /md:grid-cols-\[13rem_minmax\(0,1fr\)\]/);
  assert.match(page, /<select/);
  assert.match(page, /min-\[560px\]:hidden/);
  assert.match(page, /min-\[560px\]:block/);
  assert.doesNotMatch(page, /overflow-x-auto/);
  assert.match(page, /min-h-11/);
  assert.match(page, /SettingsSectionIcon/);
  assert.match(icons, /strokeWidth: 1\.25/);
  assert.match(icons, /currentColor/);
  for (const icon of [
    "reference-folder-papers",
    "reference-translation",
    "reference-document-tools",
    "reference-sliders",
  ]) {
    assert.match(icons, new RegExp(`data-icon="${icon}"`));
  }
  assert.match(icons, /hsl\(var\(--settings-icon\)\)/);
  assert.doesNotMatch(icons, /reader-accent/);
  assert.match(page, /activeSection === "library" \? "" : "hidden"/);
  assert.doesNotMatch(page, /decorate-bar/);
  assert.doesNotMatch(page, /AnonymousUsageSettings/);
});

test("storage settings explain the local default and expose a clear folder action", () => {
  const page = read("app/config/page.tsx");
  const library = read("components/LocalLibrarySettings.tsx");

  assert.match(page, /数据默认留在这台电脑/);
  assert.match(library, /当前保存位置/);
  assert.match(library, /这台电脑/);
  assert.match(library, /选择本地文件夹/);
  assert.match(library, /min-h-11/);
  assert.match(library, /errorName === "AbortError"/);
  assert.match(library, /未选择文件夹，保存位置没有改变/);
  assert.doesNotMatch(page, /Pet Core|Pet 本机|Mac/);
  assert.doesNotMatch(library, /Pet Core|Pet 本机|Mac/);
  assert.doesNotMatch(page, /服务器模式/);
  assert.doesNotMatch(library, /服务器模式/);
});

test("provider settings expose category-specific sections without duplicating state", () => {
  const provider = read("components/ProviderConfig.tsx");

  assert.match(provider, /export type ProviderConfigSection = "models" \| "tools" \| "advanced"/);
  assert.match(provider, /hidden=\{section !== "models"\}/);
  assert.match(provider, /hidden=\{section !== "tools"\}/);
  assert.match(provider, /hidden=\{section !== "advanced"\}/);
  assert.match(provider, /保存配置/);
  assert.doesNotMatch(provider, /decorate-bar/);
});

test("local Core no longer emits anonymous usage events from the UI", () => {
  const api = read("lib/api.ts");
  const paper = read("app/paper/[id]/page.tsx");

  assert.equal(
    fs.existsSync(path.join(frontendDir, "components/AnonymousUsageSettings.tsx")),
    false,
  );
  assert.doesNotMatch(api, /recordAnonymousUsage|getAnonymousUsageSettings/);
  assert.doesNotMatch(paper, /recordAnonymousUsage|reader_opened/);
});
