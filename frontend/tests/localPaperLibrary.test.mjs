import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return fs.readFileSync(path.join(frontendDir, relativePath), "utf8");
}

test("local folder mode is opt-in and keeps its directory handle in IndexedDB", () => {
  const source = read("lib/localPaperLibrary.ts");
  assert.match(source, /showDirectoryPicker/);
  assert.match(source, /indexedDB\.open/);
  assert.match(source, /window\.isSecureContext/);
  assert.match(source, /getLibraryMode\(\).*"server"/s);
  assert.doesNotMatch(source, /api_key|admin.*token|config\.yaml/i);
  assert.match(source, /if \(permission === "denied"\) \{\s*useServerLibrary\(\)/s);
});

test("portable writes verify every file before committing the manifest", () => {
  const source = read("lib/localPaperLibrary.ts");
  const verifyPosition = source.indexOf(
    "await writeVerifiedFile(paperDirectory, path, payload, entry)",
  );
  const manifestPosition = source.indexOf(
    'await writeJsonFile(paperDirectory, "manifest.json", manifest)',
  );
  const acknowledgementPosition = source.indexOf(
    "await acknowledgePortableCache(paperId, manifest.revision)",
  );
  assert.ok(verifyPosition > 0);
  assert.ok(manifestPosition > verifyPosition);
  assert.ok(acknowledgementPosition > manifestPosition);
  assert.match(source, /crypto\.subtle\.digest\("SHA-256"/);
  assert.match(source, /MAX_TOTAL_BYTES = 512 \* 1024 \* 1024/);
});

test("revision conflicts require an explicit local or server choice", () => {
  const library = read("lib/localPaperLibrary.ts");
  const settings = read("components/LocalLibrarySettings.tsx");
  assert.match(library, /PortableRevisionConflictError/);
  assert.match(library, /response\.status === 409/);
  assert.match(settings, />\s*保留本地\s*</);
  assert.match(settings, />\s*保留服务端\s*</);
});

test("local folder settings use clear local-first language and handle picker cancellation", () => {
  const settings = read("components/LocalLibrarySettings.tsx");

  assert.match(settings, /这台电脑/);
  assert.match(settings, />\s*\{localEnabled \? "更换文件夹" : "选择本地文件夹"\}\s*</);
  assert.match(settings, /errorName === "AbortError"/);
  assert.match(settings, /未选择文件夹，保存位置没有改变/);
  assert.doesNotMatch(settings, /Pet Core|Pet 本机|Mac/);
  assert.doesNotMatch(settings, /服务器模式/);
});

test("paper changes schedule local sync without changing server mode behavior", () => {
  const status = read("components/LocalPaperSyncStatus.tsx");
  const paperRoute = read("app/paper/[id]/page.tsx");
  const api = read("lib/api.ts");
  assert.match(status, /PAPER_DATA_CHANGED_EVENT/);
  assert.match(status, /window\.setTimeout\(\(\) => void sync\(\), 800\)/);
  assert.match(paperRoute, /LocalPaperSyncStatus/);
  assert.match(api, /notifyPaperDataChanged\(arxiv_id\)/);
});

test("active paper and Agent pages renew the bounded server cache lease", () => {
  const lease = read("lib/usePortableCacheLease.ts");
  const reader = read("components/LocalPaperSyncStatus.tsx");
  const agent = read("components/agent/AgentWorkspace.tsx");
  assert.match(lease, /renewPortableCacheLease/);
  assert.match(lease, /60_000/);
  assert.match(reader, /usePortableCacheLease\(paperId\)/);
  assert.match(agent, /usePortableCacheLease\(arxivId\)/);
});

test("missing server caches recover from the local manifest before rendering", () => {
  const reader = read("app/paper/[id]/page.tsx");
  const agent = read("components/agent/AgentWorkspace.tsx");
  assert.match(reader, /recoverPaperFromLocalIfAvailable\(arxivId\)/);
  assert.match(reader, /getPaperIfExists\(arxivId\)/);
  assert.match(agent, /recoverPaperFromLocalIfAvailable\(arxivId\)/);
  assert.match(agent, /getPaperIfExists\(arxivId\)/);
});

test("workspace migration publishes its manifest only after every paper succeeds", () => {
  const library = read("lib/localPaperLibrary.ts");
  const settings = read("components/LocalLibrarySettings.tsx");
  const syncStart = library.indexOf("export async function syncWorkspaceToLocal");
  const failedCount = library.indexOf('item.status === "error"', syncStart);
  const manifestWrite = library.indexOf("writeLocalWorkspaceManifest", failedCount);

  assert.ok(syncStart > 0);
  assert.ok(failedCount > syncStart);
  assert.ok(manifestWrite > failedCount);
  assert.match(library, /failed === 0\s*\?\s*await writeLocalWorkspaceManifest/s);
  assert.match(library, /forceFull: true/);
  assert.match(settings, /保存所选论文到此目录/);
  assert.match(settings, /从此目录恢复/);
});

test("workspace migration defaults to no papers and filters unrelated collections", () => {
  const settings = read("components/LocalLibrarySettings.tsx");

  assert.match(settings, /const \[selectedPaperIds, setSelectedPaperIds\] = useState<string\[\]>\(\[\]\)/);
  assert.match(settings, /setSelectedPaperIds\(\[\]\)/);
  assert.match(settings, /请先选择要保存到本地目录的论文/);
  assert.match(settings, /\.filter\(\(paperId\) => selected\.has\(paperId\)\)/);
  assert.match(settings, /if \(paperIds\.length\) \{\s*collections\.push/s);
  assert.match(settings, /disabled=\{busy \|\| loadingMigrationPapers \|\| !selectedPaperIds\.length\}/);
  assert.match(settings, />\s*全选\s*</);
  assert.match(settings, />\s*清空\s*</);
});

test("workspace restore verifies the root revision and each paper revision", () => {
  const source = read("lib/localPaperLibrary.ts");

  assert.match(source, /workspace_revision: await sha256/);
  assert.match(source, /actual !== manifest\.workspace_revision/);
  assert.match(source, /manifest\.revision !== expectedRevisions\.get\(paperId\)/);
  assert.match(source, /const entries = \[\.\.\.manifest\.files\];/);
  assert.doesNotMatch(
    source,
    /const entries = \[\.\.\.manifest\.files\]\.sort/,
  );
  assert.match(source, /encodeURIComponent\(manifest\.paper_id\) !== name/);
  assert.match(source, /MAX_WORKSPACE_PAPERS = 2000/);
  assert.match(source, /collection\.paper_ids\.some\(\(paperId\) => !paperIds\.has\(paperId\)\)/);
});

test("workspace migration keeps credentials and server identity out of local manifests", () => {
  const source = read("lib/localPaperLibrary.ts");
  const manifestSection = source.slice(
    source.indexOf("export type LocalWorkspaceManifest"),
    source.indexOf("export type BulkPaperMigrationItem"),
  );

  assert.doesNotMatch(
    manifestSection,
    /api[_-]?key|token|credential|server[_-]?url|base[_-]?url|file[_-]?handle/i,
  );
  assert.match(manifestSection, /papers/);
  assert.match(manifestSection, /collections/);
  assert.match(manifestSection, /workspace_revision/);
});
