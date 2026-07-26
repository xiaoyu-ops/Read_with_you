import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return fs.readFileSync(path.join(frontendDir, relativePath), "utf8");
}

test("header exposes a route-aware tutorial entry", () => {
  const header = read("components/Header.tsx");
  const styles = read("app/globals.css");

  assert.match(header, /href: "\/guide", label: "教程", active: pathname === "\/guide"/);
  assert.match(header, /aria-current=\{item\.active \? "page" : undefined\}/);
  assert.match(styles, /\.app-nav-link \{[\s\S]*white-space: nowrap/);
  assert.match(styles, /@media \(max-width: 420px\) \{[\s\S]*padding-inline: 0\.6rem/);
  assert.match(styles, /@media \(max-width: 420px\) \{[\s\S]*gap: 0\.35rem/);
});

test("guide describes the real five-step research workflow", () => {
  const guide = read("app/guide/page.tsx");

  for (const title of [
    "完成一次设置",
    "检索并确认论文",
    "在原始 PDF 上阅读",
    "把判断写进笔记",
    "让 Pet 和 Agent 接着研究",
  ]) {
    assert.match(guide, new RegExp(title));
  }

  for (const href of ["/config", "/", "/reading", "/library", "/agent"]) {
    assert.match(guide, new RegExp(`href: "${href.replace("/", "\\/")}"`));
  }

  assert.match(guide, /论文与笔记/);
  assert.match(guide, /系统凭据库/);
  assert.match(guide, /浏览器自动化和中文 PDF 导出按需启用/);
  assert.match(guide, /aria-labelledby="guide-steps-title"/);
  assert.match(guide, /aria-labelledby="guide-data-title"/);
});
