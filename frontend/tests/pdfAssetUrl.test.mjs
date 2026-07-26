import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";


const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(frontendDir, "lib", "pdfAssetUrl.ts");
const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ES2022,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
  },
  fileName: sourcePath,
  reportDiagnostics: true,
});
const errors = (transpiled.diagnostics ?? []).filter(
  (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
);
if (errors.length > 0) {
  throw new Error(
    errors.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"),
  );
}
const pdfAsset = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
);

test("keeps production paper assets outside the API proxy", () => {
  assert.equal(
    pdfAsset.resolvePdfAssetUrl("/assets/1706.03762/original.pdf", "/api"),
    "/assets/1706.03762/original.pdf",
  );
});

test("uses the API origin for local absolute API bases", () => {
  assert.equal(
    pdfAsset.resolvePdfAssetUrl(
      "/assets/local-paper/original.pdf",
      "http://127.0.0.1:8000/api/",
    ),
    "http://127.0.0.1:8000/assets/local-paper/original.pdf",
  );
});

test("keeps non-asset API paths and absolute PDF URLs compatible", () => {
  assert.equal(
    pdfAsset.resolvePdfAssetUrl("/papers/browser-fixture/pdf", "/api-mock"),
    "/api-mock/papers/browser-fixture/pdf",
  );
  assert.equal(
    pdfAsset.resolvePdfAssetUrl("https://example.test/paper.pdf", "/api"),
    "https://example.test/paper.pdf",
  );
});
