import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return fs.readFileSync(path.join(frontendDir, relativePath), "utf8");
}

test("homepage exposes persistent read and map tasks before search", () => {
  const home = read("app/page.tsx");
  const candidates = read("components/PaperCandidates.tsx");

  assert.match(home, /找论文精读/);
  assert.match(home, /看论文关系/);
  assert.match(home, /new URLSearchParams\(window\.location\.search\)\.get\("task"\)/);
  assert.match(home, /\/\?task=map/);
  assert.match(candidates, /查看图谱/);
  assert.match(candidates, /打开阅读/);
  assert.match(candidates, /可查看图谱，暂不支持站内阅读/);
  assert.match(candidates, /task === "map"/);
});

test("literature map API has a versioned typed contract and bounded request", () => {
  const api = read("lib/api.ts");

  assert.match(api, /interface LiteratureMap \{/);
  assert.match(api, /version: 1/);
  assert.match(api, /kind: "similarity" \| "citation"/);
  assert.match(api, /Math\.max\(10, Math\.min\(50/);
  assert.match(api, /encodeURIComponent\(paperRef\)/);
  assert.match(api, /`ARXIV:\$\{arxivId\}`/);
});

test("workspace separates similarity and directed citation semantics", () => {
  const workspace = read("components/literature-map/LiteratureMapWorkspace.tsx");
  const graph = read("components/literature-map/LiteratureGraph.tsx");

  for (const label of ["图谱", "先行工作", "后续工作", "列表", "筛选"]) {
    assert.match(workspace, new RegExp(label));
  }
  for (const filter of ["PDF 可用", "开放获取", "已入库"]) {
    assert.match(workspace, new RegExp(filter));
  }
  assert.match(workspace, /相似关系/);
  assert.match(workspace, /引用关系/);
  assert.match(workspace, /箭头从引用论文指向被引用论文/);
  assert.match(graph, /markerEnd=\{edge\.kind === "citation"/);
  assert.match(graph, /event\.key === "Enter" \|\| event\.key === " "/);
  assert.match(graph, /setPointerCapture/);
});

test("workspace preserves S2-only boundaries and arXiv product actions", () => {
  const workspace = read("components/literature-map/LiteratureMapWorkspace.tsx");
  const paperPage = read("app/paper/[id]/page.tsx");
  const pet = read("components/PetAssistant.tsx");

  assert.match(workspace, /selected\.arxiv_id \?/);
  assert.match(workspace, /只有外部元数据/);
  assert.match(workspace, /打开阅读/);
  assert.match(workspace, /问 Pet/);
  assert.match(workspace, /以此论文为中心展开/);
  assert.match(paperPage, /get\("pet"\) === "open"/);
  assert.match(pet, /initialOpen/);
});

test("literature map uses three columns and non-modal responsive panels", () => {
  const styles = read("app/globals.css");

  assert.match(styles, /grid-template-columns: 300px minmax\(0, 1fr\) 340px/);
  assert.match(styles, /@media \(min-width: 768px\) and \(max-width: 1279px\)/);
  assert.match(styles, /grid-template-columns: minmax\(0, 1fr\) 320px/);
  assert.match(styles, /@media \(max-width: 767px\)/);
  assert.match(styles, /\.literature-mobile-tabs/);
  assert.match(styles, /\.literature-map-list\.is-mobile-active/);
  assert.doesNotMatch(
    styles.slice(styles.indexOf("/* T26")),
    /\.literature-map-(list|detail)\s*\{[\s\S]{0,160}position:\s*fixed/,
  );
});
