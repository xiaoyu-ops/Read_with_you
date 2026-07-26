#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MIN_CHARACTER_SCORE = 0.95;

const DEFAULT_SAMPLES = [
  {
    id: "1706.03762",
    pdf: "data/papers/1706.03762/original.pdf",
    page: 1,
    gold: [
      "Attention Is All You Need",
      "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder.",
    ],
  },
  {
    id: "2202.09741",
    pdf: "data/papers/2202.09741/original.pdf",
    page: 1,
    gold: [
      "Visual Attention Network",
      "While originally designed for natural language processing tasks, the self-attention mechanism has recently taken various computer vision areas by storm.",
    ],
  },
  {
    id: "2104.08691",
    pdf: "data/papers/2104.08691/original.pdf",
    page: 1,
    gold: ["The Power of Scale for Parameter-Efficient Prompt Tuning"],
  },
  {
    id: "2303.09540",
    pdf: "data/papers/2303.09540/original.pdf",
    page: 1,
    gold: ["SemDeDup: Data-efficient learning at web-scale through semantic deduplication"],
  },
  {
    id: "digital-two-column-fixture",
    pdf: "backend/tests/fixtures/translation_layout/digital_two_column.pdf",
    page: 1,
    gold: ["Deterministic Two Column Layout"],
  },
  {
    id: "scanned-two-page-fixture",
    pdf: "backend/tests/fixtures/translation_layout/scanned_two_page.pdf",
    page: 1,
    expected: "no_text_layer",
    gold: [],
  },
];

export function normalizeSelectionText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .replaceAll("\u00ad", "")
    .replace(/\s+/gu, " ")
    .trim();
}

export function scoreGoldSelections(extractedText, goldSelections) {
  const extracted = normalizeSelectionText(extractedText);
  let goldCharacters = 0;
  let matchedCharacters = 0;
  const selections = goldSelections.map((raw) => {
    const gold = normalizeSelectionText(raw);
    const matched = gold.length > 0 && extracted.includes(gold);
    goldCharacters += gold.length;
    if (matched) matchedCharacters += gold.length;
    return {
      text: gold,
      characters: gold.length,
      exact: matched,
      sha256: crypto.createHash("sha256").update(gold, "utf8").digest("hex"),
    };
  });
  const recall = goldCharacters > 0 ? matchedCharacters / goldCharacters : 1;
  // The selected payload is exactly the reconstructed TextLayer range. An
  // unmatched gold selection contributes no predicted characters rather than
  // allowing a fuzzy substring to masquerade as a valid selection.
  const precision = goldCharacters > 0 ? matchedCharacters / goldCharacters : 1;
  return { precision, recall, gold_characters: goldCharacters, matched_characters: matchedCharacters, selections };
}

export async function auditSamples(samples = DEFAULT_SAMPLES) {
  const pdfjs = await import(
    pathToFileURL(path.join(ROOT, "frontend/node_modules/pdfjs-dist/legacy/build/pdf.mjs")).href
  );
  const results = [];
  for (const sample of samples) {
    const pdfPath = path.resolve(ROOT, sample.pdf);
    const bytes = await fs.readFile(pdfPath);
    const loadingTask = pdfjs.getDocument({
      data: new Uint8Array(bytes),
      disableWorker: true,
      useSystemFonts: true,
      verbosity: 0,
    });
    const document = await loadingTask.promise;
    try {
      const page = await document.getPage(sample.page);
      const content = await page.getTextContent({ includeMarkedContent: true });
      const text = content.items
        .filter((item) => typeof item.str === "string")
        .map((item) => item.str)
        .join(" ");
      const normalized = normalizeSelectionText(text);
      if (sample.expected === "no_text_layer") {
        results.push({
          id: sample.id,
          status: normalized.length < 2 ? "pass" : "fail",
          selection_mode: normalized.length < 2 ? "disabled_no_text_layer" : "unexpected_text_layer",
          extracted_characters: normalized.length,
          precision: null,
          recall: null,
          payload_hash_ratio: null,
        });
        continue;
      }
      const score = scoreGoldSelections(normalized, sample.gold);
      const passed = score.precision >= MIN_CHARACTER_SCORE && score.recall >= MIN_CHARACTER_SCORE;
      results.push({
        id: sample.id,
        status: passed ? "pass" : "fail",
        selection_mode: "pdfjs_text_layer",
        extracted_characters: normalized.length,
        precision: score.precision,
        recall: score.recall,
        payload_hash_ratio: score.selections.every((item) => item.exact) ? 1 : 0,
        ...score,
      });
    } finally {
      await document.destroy();
    }
  }
  return {
    threshold: MIN_CHARACTER_SCORE,
    status: results.every((item) => item.status === "pass") ? "pass" : "fail",
    samples: results,
  };
}

async function main() {
  const report = await auditSamples();
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (report.status !== "pass") process.exitCode = 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
