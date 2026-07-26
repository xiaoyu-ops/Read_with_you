import fs from "node:fs";
import path from "node:path";

import { expect, test } from "playwright/test";
import ts from "typescript";


function browserSelectionSource(): string {
  const sourcePath = path.resolve(process.cwd(), "lib/pdfTextSelection.ts");
  const transpiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
    },
    fileName: sourcePath,
  }).outputText.replace(/^export\s+/gm, "");
  return `${transpiled}\nwindow.__petPdfSelection = { serializePdfTextSelection, rangeFromTextItemAnchors };`;
}

test("serializes and reconstructs an exact same-page TextLayer range", async ({ page }) => {
  await page.goto("/");
  await page.setContent(`
    <style>
      #layer { position: absolute; left: 100px; top: 80px; width: 400px; height: 600px; }
      #layer span { position: absolute; left: 40px; height: 24px; font: 20px serif; white-space: pre; }
      #item-0 { top: 60px; }
      #item-1 { top: 90px; }
    </style>
    <div id="layer" data-pdf-text-page="1"><span id="item-0" data-text-item-index="0">Reliable </span><span id="item-1" data-text-item-index="1">selection</span></div>
  `);
  await page.addScriptTag({ content: browserSelectionSource() });

  const result = await page.evaluate(async () => {
    const layer = document.querySelector<HTMLElement>("#layer")!;
    const start = document.querySelector("#item-0")!.firstChild!;
    const end = document.querySelector("#item-1")!.firstChild!;
    const range = document.createRange();
    range.setStart(start, 2);
    range.setEnd(end, 5);
    const api = (window as unknown as {
      __petPdfSelection: {
        serializePdfTextSelection(input: Record<string, unknown>): Promise<Record<string, unknown>>;
        rangeFromTextItemAnchors(layer: HTMLElement, start: unknown, end: unknown): Range | null;
      };
    }).__petPdfSelection;
    const serialized = await api.serializePdfTextSelection({
      range,
      textLayer: layer,
      page: 1,
      sourcePdfSha256: "b".repeat(64),
      regions: [{
        region_id: "r-1",
        block_index: 7,
        page: 1,
        bbox: { x0: 0.05, y0: 0.08, x1: 0.8, y1: 0.25 },
        confidence: 0.96,
      }],
    }) as { selection?: Record<string, unknown>; error?: string };
    if (!serialized.selection) return { ...serialized, rebuilt: null };
    const rebuilt = api.rangeFromTextItemAnchors(
      layer,
      serialized.selection.start,
      serialized.selection.end,
    );
    return { ...serialized, rebuilt: rebuilt?.toString() };
  });

  expect(result.error).toBeUndefined();
  expect(result.rebuilt).toBe("liable selec");
  expect(result.selection).toMatchObject({
    version: 2,
    page: 1,
    raw_text: "liable selec",
    start: { item_index: 0, char_offset: 2 },
    end: { item_index: 1, char_offset: 5 },
    block_index: 7,
    region_id: "r-1",
  });
  expect(result.selection?.text_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(Array.isArray(result.selection?.rects)).toBe(true);
});

test("rejects a range whose endpoint leaves the current page TextLayer", async ({ page }) => {
  await page.goto("/");
  await page.setContent(`
    <div id="page-1"><span data-text-item-index="0">first page</span></div>
    <div id="page-2"><span data-text-item-index="0">second page</span></div>
  `);
  await page.addScriptTag({ content: browserSelectionSource() });
  const result = await page.evaluate(async () => {
    const layer = document.querySelector<HTMLElement>("#page-1")!;
    const range = document.createRange();
    range.setStart(layer.querySelector("span")!.firstChild!, 0);
    range.setEnd(document.querySelector("#page-2 span")!.firstChild!, 6);
    return (window as unknown as {
      __petPdfSelection: { serializePdfTextSelection(input: Record<string, unknown>): Promise<unknown> };
    }).__petPdfSelection.serializePdfTextSelection({
      range,
      textLayer: layer,
      page: 1,
      sourcePdfSha256: "b".repeat(64),
      regions: [],
    });
  });
  expect(result).toEqual({ error: "请在同一页原文中选择一段连续文字。" });
});
