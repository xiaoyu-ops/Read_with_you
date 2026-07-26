import type { Browser, Page, TestInfo } from "playwright/test";
import { expect, test } from "playwright/test";

import type {
  TextMeasureInput,
  TranslationTextMeasurer,
} from "../lib/translationFit";
import { buildTranslationFitPlan } from "../lib/translationFit";
import { createDomTranslationMeasurer } from "../lib/domTranslationMeasurer";
import type { Block, TranslationLayout, TranslationLayoutRegion } from "../lib/api";


const PDF_PAGE_WIDTH = 600;
const PDF_PAGE_HEIGHT = 800;
const CSS_PAGE_WIDTH_AT_100 = 480;
const CSS_PAGE_HEIGHT_AT_100 = 640;

const normalizedBox = (x0: number, y0: number, x1: number, y1: number) => ({
  x0,
  y0,
  x1,
  y1,
});

function textRegion(
  regionId: string,
  flowOrder: number,
  y0: number,
  page = 1,
): TranslationLayoutRegion {
  return {
    region_id: regionId,
    block_index: 0,
    page,
    flow_order: flowOrder,
    kind: "paragraph",
    bbox: normalizedBox(0.1, y0, 0.45, y0 + 0.1),
    line_boxes: [normalizedBox(0.1, y0, 0.45, y0 + 0.025)],
    word_boxes: [],
    protected_boxes: [],
    source_block_order: flowOrder,
    source_line_orders: [flowOrder],
    source_word_orders: [],
    rotation: 0,
    confidence: 0.96,
    render_policy: "replace",
    failure_reason: null,
  };
}

function fixtureLayout(regions: TranslationLayoutRegion[]): TranslationLayout {
  const pageCount = Math.max(...regions.map((region) => region.page));
  return {
    version: 1,
    cache_key: "a".repeat(64),
    source_pdf_sha256: "b".repeat(64),
    block_source_sha256: "c".repeat(64),
    adapter: "poppler_bbox_layout",
    adapter_version: "2",
    pdf_url: "/papers/typesetting/pdf",
    page_count: pageCount,
    pages: Array.from({ length: pageCount }, (_, index) => ({
      page: index + 1,
      width: PDF_PAGE_WIDTH,
      height: PDF_PAGE_HEIGHT,
      rotation: 0,
    })),
    regions,
    quality: {
      mappable_count: 1,
      mapped_count: 1,
      replaceable_count: 1,
      panel_only_count: 0,
      unmapped_count: 0,
      mapped_ratio: 1,
      average_confidence: 0.96,
      protected_overlap_count: 0,
      protected_count: 0,
      unmapped_block_indexes: [],
      failure_counts: {},
    },
    warnings: [],
  };
}

function fixtureBlock(): Block {
  return {
    index: 0,
    type: "paragraph",
    original: "A deterministic browser typesetting fixture cites [3].",
    translation: "这是用于浏览器原位排版验证的确定性中文译文，它会按阅读顺序连续流入两个区域并完整保留引用 [3]。",
    status: "done",
  };
}

async function installBrowserMeasurer(page: Page): Promise<TranslationTextMeasurer> {
  const factorySource = createDomTranslationMeasurer.toString();
  await page.evaluate((source) => {
    const factory = (0, eval)(`(${source})`) as (ownerDocument: Document) => {
      maxFittingPrefix(input: TextMeasureInput): number;
      verify(input: TextMeasureInput): boolean;
      dispose(): void;
    };
    const measurer = factory(document);
    (window as unknown as { __petTranslationMeasurer: typeof measurer }).__petTranslationMeasurer = measurer;
  }, factorySource);

  return {
    maxFittingPrefix(input) {
      return page.evaluate((request) => (
        window as unknown as {
          __petTranslationMeasurer: { maxFittingPrefix(value: TextMeasureInput): number };
        }
      ).__petTranslationMeasurer.maxFittingPrefix(request), input);
    },
    verify(input) {
      return page.evaluate((request) => (
        window as unknown as {
          __petTranslationMeasurer: { verify(value: TextMeasureInput): boolean };
        }
      ).__petTranslationMeasurer.verify(request), input);
    },
  };
}

async function disposeBrowserMeasurer(page: Page): Promise<void> {
  await page.evaluate(() => (
    window as unknown as { __petTranslationMeasurer: { dispose(): void } }
  ).__petTranslationMeasurer.dispose());
}

async function runMatrixCase(
  browser: Browser,
  viewport: { width: number; height: number },
  deviceScaleFactor: number,
  testInfo: TestInfo,
) {
  const context = await browser.newContext({ viewport, deviceScaleFactor });
  const page = await context.newPage();
  await page.setContent("<main id='fixture'></main>");
  const measurer = await installBrowserMeasurer(page);
  const regions = [
    textRegion("region-a", 0, 0.1),
    textRegion("region-b", 1, 0.1, 2),
  ];
  const protectedBox = normalizedBox(0.55, 0.1, 0.75, 0.3);
  const protectedRegion: TranslationLayoutRegion = {
    ...textRegion("protected-evidence-region", 0, 0.1),
    block_index: 9,
    kind: "image",
    bbox: protectedBox,
    line_boxes: [],
    source_line_orders: [],
    protected_boxes: [protectedBox],
    render_policy: "preserve",
    failure_reason: "protected_content",
  };
  const block = fixtureBlock();
  const plan = await buildTranslationFitPlan(
    fixtureLayout([...regions, protectedRegion]),
    [block],
    measurer,
    {
      backgroundByRegion: { "region-a": "uniform", "region-b": "uniform" },
      pageCssSizeAt100: {
        1: { widthPx: CSS_PAGE_WIDTH_AT_100, heightPx: CSS_PAGE_HEIGHT_AT_100 },
        2: { widthPx: CSS_PAGE_WIDTH_AT_100, heightPx: CSS_PAGE_HEIGHT_AT_100 },
      },
    },
  );
  expect(plan.blocks[0].policy).toBe("replace");
  expect(plan.blocks[0].regions.map((item) => item.text).join("")).toBe(block.translation);

  const zoomFingerprints: unknown[] = [];
  for (const zoom of [1, 1.5, 2]) {
    const metrics = await page.evaluate(
      ({ fittedRegions, pageWidth, pageHeight, scale, protectedGeometry }) => {
        const root = document.querySelector<HTMLElement>("#fixture")!;
        root.replaceChildren();
        root.style.width = `${pageWidth * scale}px`;
        root.style.position = "relative";
        root.style.display = "grid";
        root.style.gap = `${16 * scale}px`;

        const pageNodes = new Map<number, HTMLElement>();
        for (const pageNumber of [...new Set(fittedRegions.map((region) => region.page))]) {
          const pageNode = document.createElement("section");
          pageNode.className = "pdf-page";
          pageNode.dataset.page = String(pageNumber);
          Object.assign(pageNode.style, {
            position: "relative",
            width: `${pageWidth * scale}px`,
            height: `${pageHeight * scale}px`,
            background: "rgb(252, 252, 250)",
            overflow: "hidden",
          });
          root.appendChild(pageNode);
          pageNodes.set(pageNumber, pageNode);
        }

        for (const region of fittedRegions) {
          const pageNode = pageNodes.get(region.page)!;
          const node = document.createElement("div");
          node.className = "translation-overlay";
          node.dataset.regionId = region.regionId;
          Object.assign(node.style, {
            position: "absolute",
            boxSizing: "border-box",
            left: `${region.bbox.x0 * pageWidth * scale}px`,
            top: `${region.bbox.y0 * pageHeight * scale}px`,
            width: `${(region.bbox.x1 - region.bbox.x0) * pageWidth * scale}px`,
            height: `${(region.bbox.y1 - region.bbox.y0) * pageHeight * scale}px`,
            padding: "0",
            border: "0",
            overflow: "hidden",
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            wordBreak: "normal",
            fontFamily: '"Noto Serif SC", "Songti SC", "STSong", serif',
            fontSize: `${region.fontPx100 * scale}px`,
            lineHeight: `${region.lineHeightPx100 * scale}px`,
            background: "rgb(252, 252, 250)",
          });
          node.textContent = region.text;
          pageNode.appendChild(node);
        }

        const protectedNode = document.createElement("div");
        protectedNode.id = "protected-evidence";
        Object.assign(protectedNode.style, {
          position: "absolute",
          left: `${protectedGeometry.x0 * pageWidth * scale}px`,
          top: `${protectedGeometry.y0 * pageHeight * scale}px`,
          width: `${(protectedGeometry.x1 - protectedGeometry.x0) * pageWidth * scale}px`,
          height: `${(protectedGeometry.y1 - protectedGeometry.y0) * pageHeight * scale}px`,
          background: "rgb(30, 30, 30)",
        });
        pageNodes.get(1)!.appendChild(protectedNode);

        const protectedRect = protectedNode.getBoundingClientRect();
        return Array.from(root.querySelectorAll<HTMLElement>(".translation-overlay")).map((node) => {
          const rect = node.getBoundingClientRect();
          const pageRect = node.parentElement!.getBoundingClientRect();
          const intersectsProtected =
            Math.min(rect.right, protectedRect.right) > Math.max(rect.left, protectedRect.left) &&
            Math.min(rect.bottom, protectedRect.bottom) > Math.max(rect.top, protectedRect.top);
          return {
            regionId: node.dataset.regionId,
            text: node.textContent,
            page: Number(node.parentElement!.dataset.page),
            normalizedLeft: (rect.left - pageRect.left) / (pageWidth * scale),
            normalizedTop: (rect.top - pageRect.top) / (pageHeight * scale),
            normalizedWidth: rect.width / (pageWidth * scale),
            normalizedHeight: rect.height / (pageHeight * scale),
            fontPx100: Number.parseFloat(getComputedStyle(node).fontSize) / scale,
            overflowX: node.scrollWidth - node.clientWidth,
            overflowY: node.scrollHeight - node.clientHeight,
            intersectsProtected,
          };
        });
      },
      {
        fittedRegions: plan.blocks[0].regions,
        pageWidth: CSS_PAGE_WIDTH_AT_100,
        pageHeight: CSS_PAGE_HEIGHT_AT_100,
        scale: zoom,
        protectedGeometry: protectedBox,
      },
    );
    for (const item of metrics) {
      expect(item.overflowX).toBeLessThanOrEqual(1);
      expect(item.overflowY).toBeLessThanOrEqual(1);
      expect(item.intersectsProtected).toBe(false);
    }
    zoomFingerprints.push(
      metrics.map(({ regionId, text, page, normalizedLeft, normalizedTop, normalizedWidth, normalizedHeight, fontPx100 }) => ({
        regionId,
        text,
        page,
        normalizedLeft: Number(normalizedLeft.toFixed(4)),
        normalizedTop: Number(normalizedTop.toFixed(4)),
        normalizedWidth: Number(normalizedWidth.toFixed(4)),
        normalizedHeight: Number(normalizedHeight.toFixed(4)),
        fontPx100: Number(fontPx100.toFixed(3)),
      })),
    );
  }
  expect(zoomFingerprints[1]).toEqual(zoomFingerprints[0]);
  expect(zoomFingerprints[2]).toEqual(zoomFingerprints[0]);

  const tiny = textRegion("tiny", 0, 0.4);
  tiny.bbox = normalizedBox(0.1, 0.4, 0.12, 0.405);
  tiny.line_boxes = [tiny.bbox];
  const failed = await buildTranslationFitPlan(
    fixtureLayout([tiny]),
    [block],
    measurer,
    {
      backgroundByRegion: { tiny: "uniform" },
      pageCssSizeAt100: {
        1: { widthPx: CSS_PAGE_WIDTH_AT_100, heightPx: CSS_PAGE_HEIGHT_AT_100 },
      },
    },
  );
  expect(failed.blocks[0].reason).toBe("overflow");
  expect(failed.blocks[0].regions).toEqual([]);

  if (viewport.width === 1440 && deviceScaleFactor === 2) {
    await page.screenshot({ path: testInfo.outputPath("typesetting-desktop-dpr2.png"), fullPage: true });
  }
  if (viewport.width === 390 && deviceScaleFactor === 2) {
    await page.screenshot({ path: testInfo.outputPath("typesetting-mobile-dpr2.png"), fullPage: true });
  }
  await disposeBrowserMeasurer(page);
  await context.close();
  return {
    policy: plan.blocks[0].policy,
    reason: plan.blocks[0].reason,
    sourceFontPx100: plan.blocks[0].sourceFontPx100,
    regions: plan.blocks[0].regions.map((item) => ({
      text: item.text,
      fontPx100: item.fontPx100,
      bbox: item.bbox,
    })),
  };
}

test("translation fit is stable across zoom, DPR and desktop/mobile CSS viewports", async ({ browser }, testInfo) => {
  const fingerprints = [];
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    for (const deviceScaleFactor of [1, 2]) {
      fingerprints.push(await runMatrixCase(browser, viewport, deviceScaleFactor, testInfo));
    }
  }
  for (const fingerprint of fingerprints.slice(1)) expect(fingerprint).toEqual(fingerprints[0]);
});
