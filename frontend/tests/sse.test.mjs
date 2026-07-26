import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


async function loadModule() {
  const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const sourcePath = path.join(frontendDir, "lib", "sse.ts");
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), "pet-sse-"));
  const outputPath = path.join(outputDir, "sse.mjs");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics ?? []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors.map((item) => ts.flattenDiagnosticMessageText(item.messageText, "\n")), []);
  const output = transpiled.outputText.replace(
    /import \{ translateStreamUrl \} from "\.\/api";/,
    'const translateStreamUrl = (id) => `http://test/translate/${id}`;',
  );
  assert.equal(output.includes('from "./api"'), false);
  fs.writeFileSync(outputPath, output, "utf8");
  try {
    return await import(`${pathToFileURL(outputPath).href}?v=${Date.now()}`);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
}

const sse = await loadModule();

function responseFromTextChunks(chunks, { status = 200 } = {}) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status, headers: { "content-type": "text/event-stream" } });
}

test("incremental parser accepts arbitrary chunk boundaries and CRLF", () => {
  const parser = new sse.IncrementalSSEParser();
  assert.deepEqual(parser.push("event: block_do"), []);
  assert.deepEqual(parser.push("ne\r\ndata: {\"index\":"), []);
  assert.deepEqual(parser.push("1,\"translation\":\"译文\"}\r\n\r"), []);
  assert.deepEqual(parser.push("\n"), [{
    event: "block_done",
    data: { index: 1, translation: "译文" },
  }]);
  assert.deepEqual(parser.finish(), []);
});

test("parser joins data lines and flushes a final unterminated event", () => {
  const parser = new sse.IncrementalSSEParser();
  parser.push("\uFEFF: comment\nevent: complete\ndata: {\"translated\":\ndata: 2}");
  assert.deepEqual(parser.finish(), [{ event: "complete", data: { translated: 2 } }]);
});

test("parser preserves malformed payloads as raw data", () => {
  const parser = new sse.IncrementalSSEParser();
  assert.deepEqual(parser.push("event: note\ndata: not-json\n\n"), [{
    event: "note",
    data: { raw: "not-json" },
  }]);
});

test("streamTranslation decodes chunks and records complete terminal event", async () => {
  const events = [];
  const errors = [];
  const result = await sse.streamTranslation(
    "paper-1",
    (event) => events.push(event),
    (error) => errors.push(error.message),
    {
      fetchImpl: async () => responseFromTextChunks([
        "event: block_done\ndata: {\"index\":0,\"translation\":\"第一段\"}\n\n",
        "event: complete\ndata: {\"translated\":1}\n\n",
      ]),
    },
  );
  assert.deepEqual(events.map((event) => event.event), ["block_done", "complete"]);
  assert.deepEqual(errors, []);
  assert.deepEqual(result, { terminalEvent: "complete", aborted: false, eventCount: 2 });
});

test("streamTranslation accepts done terminal compatibility", async () => {
  const result = await sse.streamTranslation("paper-1", () => {}, undefined, {
    fetchImpl: async () => responseFromTextChunks([
      "event: done\ndata: {\"translated\":0}\n\n",
    ]),
  });
  assert.equal(result.terminalEvent, "done");
});

test("terminal event wins, stops reading, and ignores later events in the same chunk", async () => {
  const encoder = new TextEncoder();
  let reads = 0;
  let cancels = 0;
  const events = [];
  const result = await sse.streamTranslation("paper-1", (event) => events.push(event.event), undefined, {
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            reads += 1;
            if (reads > 1) throw new DOMException("Aborted", "AbortError");
            return {
              done: false,
              value: encoder.encode(
                "event: complete\ndata: {}\n\nevent: block_done\ndata: {\"index\":9,\"translation\":\"late\"}\n\n",
              ),
            };
          },
          cancel: async () => {
            cancels += 1;
          },
        }),
      },
    }),
  });
  assert.deepEqual(result, { terminalEvent: "complete", aborted: false, eventCount: 1 });
  assert.deepEqual(events, ["complete"]);
  assert.equal(reads, 1);
  assert.equal(cancels, 1);
});

test("stream ending before terminal reports deterministic interruption", async () => {
  const errors = [];
  const result = await sse.streamTranslation("paper-1", () => {}, (error) => {
    errors.push(error.message);
  }, {
    fetchImpl: async () => responseFromTextChunks([
      "event: block_error\ndata: {\"index\":0}\n\n",
    ]),
  });
  assert.equal(result.terminalEvent, null);
  assert.equal(result.aborted, false);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /完成事件前中断/);
});

test("AbortSignal stops the stream without surfacing a connection error", async () => {
  const controller = new AbortController();
  controller.abort();
  const errors = [];
  const result = await sse.streamTranslation("paper-1", () => {}, (error) => {
    errors.push(error.message);
  }, {
    signal: controller.signal,
    fetchImpl: async () => {
      throw new DOMException("Aborted", "AbortError");
    },
  });
  assert.deepEqual(result, { terminalEvent: null, aborted: true, eventCount: 0 });
  assert.deepEqual(errors, []);
});

test("a stream that closes cleanly after abort is still reported as aborted", async () => {
  const controller = new AbortController();
  const errors = [];
  const result = await sse.streamTranslation("paper-1", () => {}, (error) => {
    errors.push(error.message);
  }, {
    signal: controller.signal,
    fetchImpl: async () => {
      controller.abort();
      return responseFromTextChunks([]);
    },
  });
  assert.deepEqual(result, { terminalEvent: null, aborted: true, eventCount: 0 });
  assert.deepEqual(errors, []);
});
