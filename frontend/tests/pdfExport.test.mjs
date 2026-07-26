import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";


const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(frontendDir, "lib", "api.ts");
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
assert.deepEqual(
  errors.map((item) => ts.flattenDiagnosticMessageText(item.messageText, "\n")),
  [],
);

const previousApiBase = process.env.NEXT_PUBLIC_API_BASE;
process.env.NEXT_PUBLIC_API_BASE = "/api/";
const pdfExport = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
);
if (previousApiBase === undefined) delete process.env.NEXT_PUBLIC_API_BASE;
else process.env.NEXT_PUBLIC_API_BASE = previousApiBase;

test("encodes paper and run identifiers in direct PDF download URLs", () => {
  assert.equal(
    pdfExport.originalPdfDownloadUrl("local/论文 ?"),
    "/api/papers/local%2F%E8%AE%BA%E6%96%87%20%3F/original-pdf/download",
  );
  assert.equal(
    pdfExport.translatedPdfDownloadUrl("local/论文 ?", "run/一号 ?"),
    "/api/papers/local%2F%E8%AE%BA%E6%96%87%20%3F/pdf-exports/run%2F%E4%B8%80%E5%8F%B7%20%3F/download",
  );
  assert.equal(
    pdfExport.pdfExportNoticeUrl("/pdf-exports/third-party-notice"),
    "/api/pdf-exports/third-party-notice",
  );
  assert.equal(
    pdfExport.pdfExportNoticeUrl("https://example.test/notice"),
    "https://example.test/notice",
  );
  assert.equal(
    pdfExport.pdfExportSourceUrl("/pdf-exports/wrapper-source"),
    "/api/pdf-exports/wrapper-source",
  );
  assert.equal(
    pdfExport.pdfExportSourceUrl("https://example.test/modified-source"),
    "https://example.test/modified-source",
  );
});

test("uses plain Chinese copy for disabled capability reasons", () => {
  assert.equal(
    pdfExport.pdfExportUnavailableMessage("license_disclosure_incomplete"),
    "第三方许可证与源码披露尚未完成，因此中文 PDF 导出保持关闭。",
  );
  assert.equal(
    pdfExport.pdfExportUnavailableMessage("sidecar_not_configured"),
    "当前部署尚未配置中文 PDF 导出服务。",
  );
  assert.equal(
    pdfExport.pdfExportUnavailableMessage("unknown_internal_code"),
    "当前部署暂不提供中文 PDF 导出，原始 PDF 仍可正常下载。",
  );
});

test("turns stable export failure codes into actionable copy", () => {
  assert.equal(
    pdfExport.pdfExportFailureMessage("sidecar_rate_limited", "technical payload"),
    "翻译服务当前繁忙，请稍后重试。",
  );
  assert.equal(
    pdfExport.pdfExportFailureMessage("export_timeout", null),
    "生成时间超过限制，本次任务已停止。",
  );
  assert.equal(
    pdfExport.pdfExportFailureMessage("backend_restarted", null),
    "服务重启中断了本次生成，请重新开始。",
  );
  assert.equal(
    pdfExport.pdfExportFailureMessage("source_pdf_missing", null),
    "原始 PDF 缺失，请重新导入论文后再生成。",
  );
  assert.equal(
    pdfExport.pdfExportFailureMessage("legacy_output_quarantined", null),
    "这份旧导出未通过当前安全证明，请重新生成中文 PDF。",
  );
  assert.equal(
    pdfExport.pdfExportFailureMessage(null, null),
    "中文 PDF 生成失败，原始 PDF 和网页译文不受影响。",
  );
});

test("sends the configured admin token only for export mutations", async () => {
  const nativeFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ id: "run-1", status: "queued" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    await pdfExport.createPdfExport("paper-1", "  admin-token  ");
    await pdfExport.cancelPdfExport("paper-1", "run-1", "admin-token");
    await pdfExport.createPdfExport("paper-2");
    assert.deepEqual(calls.map((call) => ({
      method: call.init.method,
      token: new Headers(call.init.headers).get("X-Peinidu-Admin-Token"),
    })), [
      { method: "POST", token: "admin-token" },
      { method: "POST", token: "admin-token" },
      { method: "POST", token: null },
    ]);
  } finally {
    globalThis.fetch = nativeFetch;
  }
});

test("maps admin authentication failures to a clear settings action", async () => {
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: "管理员令牌无效",
  }), {
    status: 401,
    headers: { "content-type": "application/json" },
  });
  try {
    await assert.rejects(
      () => pdfExport.createPdfExport("paper-1", "wrong-token"),
      (error) => {
        assert.equal(error.code, "admin_required");
        assert.equal(error.retryable, false);
        assert.match(error.message, /设置/);
        return true;
      },
    );
  } finally {
    globalThis.fetch = nativeFetch;
  }
});

test("preserves structured backend error code and retryability", async () => {
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: {
      code: "page_limit_exceeded",
      message: "backend limit detail",
      retryable: false,
    },
  }), {
    status: 413,
    headers: { "content-type": "application/json" },
  });
  try {
    await assert.rejects(
      () => pdfExport.createPdfExport("paper/with slash"),
      (error) => {
        assert.equal(error.code, "page_limit_exceeded");
        assert.equal(error.message, "backend limit detail");
        assert.equal(error.retryable, false);
        return true;
      },
    );
  } finally {
    globalThis.fetch = nativeFetch;
  }
});
