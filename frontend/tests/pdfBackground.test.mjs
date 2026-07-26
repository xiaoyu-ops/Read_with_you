import assert from "node:assert/strict";
import test from "node:test";

import ts from "typescript";

const sourcePath = new URL("../lib/pdfBackground.ts", import.meta.url);
const source = await (await import("node:fs/promises")).readFile(sourcePath, "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ES2022,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
  },
  fileName: sourcePath.pathname,
  reportDiagnostics: true,
});
const errors = (transpiled.diagnostics ?? []).filter(
  (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
);
assert.equal(
  errors.length,
  0,
  errors.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"),
);
const pdfBackground = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
);

function pixels(width, height, pixelAt) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4;
      const [red, green, blue, alpha = 255] = pixelAt(x, y);
      data.set([red, green, blue, alpha], offset);
    }
  }
  return { data, width, height };
}

function samplingCanvas(sourceWidth, sourceHeight, pixelAt) {
  const sampledSizes = [];
  const ownerDocument = {
    createElement() {
      let drawCall = null;
      const sampleCanvas = {
        width: 0,
        height: 0,
        getContext() {
          return {
            imageSmoothingEnabled: true,
            clearRect() {},
            drawImage(...args) {
              drawCall = args;
            },
            getImageData() {
              sampledSizes.push([sampleCanvas.width, sampleCanvas.height]);
              return pixels(sampleCanvas.width, sampleCanvas.height, (x, y) => {
                const sourceX = drawCall?.[1] ?? 0;
                const sourceY = drawCall?.[2] ?? 0;
                const sampledSourceWidth = drawCall?.[3] ?? sourceWidth;
                const sampledSourceHeight = drawCall?.[4] ?? sourceHeight;
                return pixelAt(x, y, {
                  sourceX:
                    sourceX + ((x + 0.5) * sampledSourceWidth) / sampleCanvas.width,
                  sourceY:
                    sourceY + ((y + 0.5) * sampledSourceHeight) / sampleCanvas.height,
                  sampledWidth: sampleCanvas.width,
                  sampledHeight: sampleCanvas.height,
                });
              });
            },
          };
        },
      };
      return sampleCanvas;
    },
  };
  return {
    canvas: { width: sourceWidth, height: sourceHeight, ownerDocument },
    sampledSizes,
  };
}

test("classifies a dominant paper color while tolerating sparse text pixels", () => {
  const sample = pixels(12, 12, (x, y) =>
    x % 5 === 1 && y % 5 === 1 ? [34, 36, 40, 255] : [247, 248, 250, 255],
  );
  const result = pdfBackground.classifyPdfBackgroundPixels(sample);

  assert.equal(result.evidence, "uniform", JSON.stringify(result));
  assert.equal(result.reason, null);
  assert.deepEqual(result.background, { r: 247, g: 248, b: 250 });
  assert.equal(result.backgroundColor, "rgb(247, 248, 250)");
  assert.equal(result.foregroundColor, "rgb(24, 25, 29)");
  assert.ok(result.contrastRatio >= 4.5);
  assert.ok(result.dominantRatio > 0.8);
});

test("recognizes a tight text box by its corners and ignores glyph antialiasing", () => {
  const sample = pixels(20, 20, (x, y) => {
    const localX = (x - 2) % 3;
    const localY = (y - 2) % 3;
    const insideGlyph = x >= 2 && x < 18 && y >= 2 && y < 18 && localX < 2 && localY < 2;
    if (!insideGlyph) return [250, 250, 250, 255];
    if (localX === 0) return [24, 24, 24, 255];
    if (localX === 1) return [170, 170, 170, 255];
    return [250, 250, 250, 255];
  });
  const result = pdfBackground.classifyPdfBackgroundPixels(sample);

  assert.ok(result.dominantRatio < 0.72);
  assert.ok(result.cornerDominantRatio >= 0.6);
  assert.equal(result.evidence, "uniform", JSON.stringify(result));
  assert.equal(result.reason, null);
});

test("rejects sparse high-contrast axes and connected line art on a white page", () => {
  const sample = pixels(32, 20, (x, y) => {
    const horizontalAxis = y === 16 && x >= 3 && x <= 29;
    const verticalAxis = x === 3 && y >= 3 && y <= 16;
    const risingLine = x >= 7 && x <= 26 && y === 15 - Math.floor((x - 7) / 3);
    return horizontalAxis || verticalAxis || risingLine
      ? [20, 20, 20, 255]
      : [250, 250, 250, 255];
  });
  const result = pdfBackground.classifyPdfBackgroundPixels(sample);

  assert.equal(result.evidence, "complex");
  assert.equal(result.reason, "high_contrast_graphic");
  assert.ok(result.dominantRatio > 0.85);
});

test("rejects a medium-length high-contrast rule", () => {
  const result = pdfBackground.classifyPdfBackgroundPixels(
    pixels(40, 20, (x, y) =>
      y === 10 && x >= 10 && x < 30 ? [20, 20, 20, 255] : [250, 250, 250, 255],
    ),
    40,
    20,
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "high_contrast_graphic");
});

test("trusted single-line text still rejects an isolated vertical axis and rules", () => {
  const verticalStem = pixels(40, 20, (x, y) =>
    x === 20 && y >= 3 && y < 17 ? [20, 20, 20, 255] : [250, 250, 250, 255],
  );
  assert.equal(
    pdfBackground.classifyPdfBackgroundPixels(verticalStem).evidence,
    "complex",
  );
  const trustedStem = pdfBackground.classifyPdfBackgroundPixels(verticalStem, {
    trustedSingleLineText: true,
  });
  assert.equal(trustedStem.evidence, "complex", JSON.stringify(trustedStem));
  assert.equal(trustedStem.reason, "high_contrast_graphic");

  const connectedBoldWord = pixels(40, 20, (x, y) => {
    const glyphStroke =
      x >= 10 &&
      x <= 25 &&
      y >= 5 + Math.floor((x - 10) / 2) &&
      y <= 6 + Math.floor((x - 10) / 2);
    return glyphStroke
      ? [20, 20, 20, 255]
      : [250, 250, 250, 255];
  });
  assert.equal(
    pdfBackground.classifyPdfBackgroundPixels(connectedBoldWord).evidence,
    "complex",
  );
  const trustedWord = pdfBackground.classifyPdfBackgroundPixels(connectedBoldWord, {
    trustedSingleLineText: true,
  });
  assert.equal(trustedWord.evidence, "complex", JSON.stringify(trustedWord));

  const horizontalRule = pixels(40, 20, (x, y) =>
    y === 10 && x >= 10 && x < 30 ? [20, 20, 20, 255] : [250, 250, 250, 255],
  );
  assert.equal(
    pdfBackground.classifyPdfBackgroundPixels(horizontalRule, {
      trustedSingleLineText: true,
    }).evidence,
    "complex",
  );
});

test("trusted text uses a bounded high-resolution confirmation without relaxing rules", () => {
  const lowResolutionWord = pixels(72, 12, (x, y) => {
    const strokeY = 2 + Math.floor((x - 20) / 4);
    const connectedStroke = x >= 20 && x <= 50 && y >= strokeY && y <= strokeY + 1;
    return connectedStroke ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const highResolutionWord = pixels(120, 20, (x, y) => {
    const separatedGlyphStem = x % 12 >= 3 && x % 12 <= 4 && y >= 4 && y <= 15;
    return separatedGlyphStem ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const sampledSizes = [];
  const ownerDocument = {
    createElement() {
      const sampleCanvas = {
        width: 0,
        height: 0,
        getContext() {
          return {
            imageSmoothingEnabled: true,
            clearRect() {},
            drawImage() {},
            getImageData() {
              sampledSizes.push([sampleCanvas.width, sampleCanvas.height]);
              if (sampleCanvas.width === 72) return lowResolutionWord;
              if (sampleCanvas.width === 120) return highResolutionWord;
              throw new Error("unexpected confirmation size");
            },
          };
        },
      };
      return sampleCanvas;
    },
  };
  const sourceCanvas = { width: 240, height: 40, ownerDocument };
  const trusted = pdfBackground.sampleCanvasRegionBackground(
    sourceCanvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    { trustedSingleLineText: true },
  );
  assert.equal(trusted.evidence, "complex", JSON.stringify(trusted));
  assert.equal(trusted.reason, "high_contrast_graphic");
  assert.deepEqual(sampledSizes, [[72, 12], [120, 20]]);

  sampledSizes.length = 0;
  const untrusted = pdfBackground.sampleCanvasRegionBackground(
    sourceCanvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
  );
  assert.equal(untrusted.evidence, "complex", JSON.stringify(untrusted));
  assert.deepEqual(sampledSizes, [[72, 12]]);
});

test("trusted high-resolution confirmation still rejects a horizontal rule", () => {
  const sampledSizes = [];
  const ownerDocument = {
    createElement() {
      const sampleCanvas = {
        width: 0,
        height: 0,
        getContext() {
          return {
            imageSmoothingEnabled: true,
            clearRect() {},
            drawImage() {},
            getImageData() {
              sampledSizes.push([sampleCanvas.width, sampleCanvas.height]);
              return pixels(sampleCanvas.width, sampleCanvas.height, (x, y) => {
                const rule =
                  y === Math.floor(sampleCanvas.height / 2) &&
                  x >= Math.floor(sampleCanvas.width * 0.2) &&
                  x < Math.ceil(sampleCanvas.width * 0.8);
                return rule ? [20, 20, 20, 255] : [250, 250, 250, 255];
              });
            },
          };
        },
      };
      return sampleCanvas;
    },
  };
  const result = pdfBackground.sampleCanvasRegionBackground(
    { width: 240, height: 40, ownerDocument },
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    { trustedSingleLineText: true },
  );
  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "high_contrast_graphic");
  assert.deepEqual(sampledSizes, [[72, 12], [120, 20]]);
});

test("trusted word geometry still fails closed when no background ring exists", () => {
  const { canvas, sampledSizes } = samplingCanvas(240, 40, (x, y) => {
    const width = sampledSizes.at(-1)?.[0] ?? 0;
    if (width <= 72) {
      const strokeY = 2 + Math.floor((x - 20) / 4);
      const joinedGlyphs = x >= 20 && x <= 50 && y >= strokeY && y <= strokeY + 1;
      return joinedGlyphs ? [20, 20, 20, 255] : [250, 250, 250, 255];
    }
    const glyphStem = x % 12 >= 3 && x % 12 <= 4 && y >= 4 && y <= 15;
    return glyphStem ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    {
      trustedTextLineBoxes: [{ x0: 0, y0: 0.05, x1: 1, y1: 0.95 }],
      trustedTextWordBoxes: [{ x0: 0, y0: 0.05, x1: 1, y1: 0.95 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "high_contrast_graphic");
  assert.deepEqual(sampledSizes, [[72, 12], [120, 20]]);
});

test("trusted multi-line text stays constrained to authoritative line boxes", () => {
  const lineBoxes = [
    { x0: 0.08, y0: 0.08, x1: 0.92, y1: 0.3 },
    { x0: 0.08, y0: 0.39, x1: 0.92, y1: 0.61 },
    { x0: 0.08, y0: 0.7, x1: 0.92, y1: 0.92 },
  ];
  const { canvas, sampledSizes } = samplingCanvas(240, 100, (x, y) => {
    const [sampleWidth, sampleHeight] = sampledSizes.at(-1);
    const normalizedX = x / sampleWidth;
    const normalizedY = y / sampleHeight;
    const insideLine = lineBoxes.some(
      (line) =>
        normalizedX >= line.x0 &&
        normalizedX <= line.x1 &&
        normalizedY >= line.y0 &&
        normalizedY <= line.y1,
    );
    const glyphStem = insideLine && x % 11 >= 3 && x % 11 <= 4;
    return glyphStem ? [22, 22, 22, 255] : [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    { trustedTextLineBoxes: lineBoxes, protectedBoxes: [] },
  );

  assert.equal(result.evidence, "uniform", JSON.stringify(result));
});

test("a strict white background ring cannot bless ambiguous connected line art", () => {
  const { canvas, sampledSizes } = samplingCanvas(240, 100, (_x, _y, sample) => {
    const diagonalY = 40 + ((sample.sourceX - 85) * 20) / 70;
    const connectedGlyph =
      sample.sourceX >= 85 &&
      sample.sourceX <= 155 &&
      Math.abs(sample.sourceY - diagonalY) <= 1.4;
    return connectedGlyph ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0.25, y0: 0.35, x1: 0.75, y1: 0.65 },
    {
      trustedTextLineBoxes: [{ x0: 0.28, y0: 0.37, x1: 0.72, y1: 0.63 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "high_contrast_graphic");
  assert.ok(sampledSizes.length >= 3, JSON.stringify(sampledSizes));
});

test("trusted line geometry rejects contained axes, formulas and icons after ring confirmation", () => {
  const shapes = {
    axis: (sourceX, sourceY) => Math.abs(sourceX - 120) <= 1 && sourceY >= 38 && sourceY <= 62,
    parallel_axes: (sourceX, sourceY) =>
      (Math.abs(sourceX - 114) <= 1 || Math.abs(sourceX - 126) <= 1) &&
      sourceY >= 38 &&
      sourceY <= 62,
    formula: (sourceX, sourceY) =>
      (Math.abs(sourceX - 120) <= 1 && sourceY >= 38 && sourceY <= 62) ||
      (Math.abs(sourceY - 50) <= 1 && sourceX >= 108 && sourceX <= 132),
    icon: (sourceX, sourceY) =>
      sourceX >= 108 &&
      sourceX <= 132 &&
      sourceY >= 38 &&
      sourceY <= 62 &&
      (Math.abs(sourceX - 108) <= 1 ||
        Math.abs(sourceX - 132) <= 1 ||
        Math.abs(sourceY - 38) <= 1 ||
        Math.abs(sourceY - 62) <= 1),
  };
  for (const [name, shape] of Object.entries(shapes)) {
    const { canvas } = samplingCanvas(240, 100, (_x, _y, sample) =>
      shape(sample.sourceX, sample.sourceY)
        ? [20, 20, 20, 255]
        : [250, 250, 250, 255],
    );
    const result = pdfBackground.sampleCanvasRegionBackground(
      canvas,
      { x0: 0.4, y0: 0.3, x1: 0.6, y1: 0.7 },
      {
        trustedTextLineBoxes: [{ x0: 0.41, y0: 0.32, x1: 0.59, y1: 0.68 }],
        trustedTextWordBoxes: [{ x0: 0.41, y0: 0.32, x1: 0.43, y1: 0.68 }],
        protectedBoxes: [],
      },
    );
    assert.equal(result.evidence, "complex", `${name}: ${JSON.stringify(result)}`);
    assert.equal(result.reason, "high_contrast_graphic", name);
  }
});

test("an authoritative word box confirms an edge-touching numeric glyph without contaminating its ring", () => {
  const { canvas } = samplingCanvas(240, 100, (_x, _y, sample) => {
    const digitOne =
      (Math.abs(sample.sourceX - 120) <= 1 && sample.sourceY >= 30 && sample.sourceY < 70) ||
      (sample.sourceX >= 108 && sample.sourceX < 132 && sample.sourceY >= 30 && sample.sourceY < 31);
    return digitOne ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const region = { x0: 0.451, y0: 0.303, x1: 0.549, y1: 0.697 };
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    region,
    {
      trustedTextLineBoxes: [region],
      trustedTextWordBoxes: [region],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "uniform", JSON.stringify(result));
});

test("word-backed glyph evidence does not hide shading outside the word box", () => {
  const { canvas } = samplingCanvas(240, 100, (_x, _y, sample) => {
    const glyph = Math.abs(sample.sourceX - 120) <= 1 && sample.sourceY >= 38 && sample.sourceY <= 62;
    const outsideShading =
      sample.sourceX >= 70 &&
      sample.sourceX <= 95 &&
      sample.sourceY >= 38 &&
      sample.sourceY <= 62;
    if (glyph) return [20, 20, 20, 255];
    if (outsideShading) return [205, 205, 205, 255];
    return [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0.25, y0: 0.3, x1: 0.75, y1: 0.7 },
    {
      trustedTextLineBoxes: [{ x0: 0.28, y0: 0.32, x1: 0.72, y1: 0.68 }],
      trustedTextWordBoxes: [{ x0: 0.46, y0: 0.34, x1: 0.54, y1: 0.66 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "spatial_nonuniform");
});

test("word-backed glyph evidence rejects a five-percent low-contrast shadow outside the word box", () => {
  const { canvas } = samplingCanvas(240, 100, (_x, _y, sample) => {
    const glyph = Math.abs(sample.sourceX - 120) <= 1 && sample.sourceY >= 38 && sample.sourceY <= 62;
    const outsideShadow =
      sample.sourceX >= 70 &&
      sample.sourceX <= 80 &&
      sample.sourceY >= 38 &&
      sample.sourceY <= 62;
    if (glyph) return [20, 20, 20, 255];
    if (outsideShadow) return [205, 205, 205, 255];
    return [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0.25, y0: 0.3, x1: 0.75, y1: 0.7 },
    {
      trustedTextLineBoxes: [{ x0: 0.28, y0: 0.32, x1: 0.72, y1: 0.68 }],
      trustedTextWordBoxes: [{ x0: 0.46, y0: 0.34, x1: 0.54, y1: 0.66 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "spatial_nonuniform");
});

test("rejects trusted word geometry outside its authoritative line", () => {
  const result = pdfBackground.sampleCanvasRegionBackground(
    { width: 100, height: 100 },
    { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.9 },
    {
      trustedTextLineBoxes: [{ x0: 0.2, y0: 0.2, x1: 0.8, y1: 0.4 }],
      trustedTextWordBoxes: [{ x0: 0.2, y0: 0.5, x1: 0.4, y1: 0.6 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "unknown");
  assert.equal(result.reason, "invalid_region");
});

test("a uniform ring cannot bless connected graphics outside trusted lines", () => {
  const { canvas } = samplingCanvas(240, 100, (_x, _y, sample) => {
    const diagonalY = 40 + ((sample.sourceX - 85) * 20) / 70;
    const connectedGraphic =
      sample.sourceX >= 85 &&
      sample.sourceX <= 155 &&
      Math.abs(sample.sourceY - diagonalY) <= 1.4;
    return connectedGraphic ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0.25, y0: 0.35, x1: 0.75, y1: 0.65 },
    {
      trustedTextLineBoxes: [{ x0: 0.28, y0: 0.37, x1: 0.38, y1: 0.63 }],
      protectedBoxes: [],
    },
  );

  assert.equal(result.evidence, "complex", JSON.stringify(result));
  assert.equal(result.reason, "high_contrast_graphic");
});

test("trusted text geometry still rejects rules and graphics outside text lines", () => {
  const horizontalRule = samplingCanvas(160, 40, (x, y) => {
    const [sampleWidth, sampleHeight] = horizontalRule.sampledSizes.at(-1);
    const rule =
      y === Math.floor(sampleHeight / 2) &&
      x >= Math.floor(sampleWidth * 0.2) &&
      x <= Math.ceil(sampleWidth * 0.8);
    return rule ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const ruleResult = pdfBackground.sampleCanvasRegionBackground(
    horizontalRule.canvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    {
      trustedTextLineBoxes: [{ x0: 0.1, y0: 0.15, x1: 0.9, y1: 0.85 }],
      protectedBoxes: [],
    },
  );
  assert.equal(ruleResult.evidence, "complex", JSON.stringify(ruleResult));
  assert.equal(ruleResult.reason, "high_contrast_graphic");

  const outsideGraphic = samplingCanvas(160, 80, (x, y) => {
    const [sampleWidth, sampleHeight] = outsideGraphic.sampledSizes.at(-1);
    const glyphStem =
      x % 10 >= 3 &&
      x % 10 <= 4 &&
      y >= Math.floor(sampleHeight * 0.08) &&
      y <= Math.ceil(sampleHeight * 0.25);
    const graphic =
      x >= Math.floor(sampleWidth * 0.45) &&
      x <= Math.ceil(sampleWidth * 0.55) &&
      y >= Math.floor(sampleHeight * 0.7) &&
      y <= Math.ceil(sampleHeight * 0.8);
    return glyphStem || graphic ? [20, 20, 20, 255] : [250, 250, 250, 255];
  });
  const graphicResult = pdfBackground.sampleCanvasRegionBackground(
    outsideGraphic.canvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
    {
      trustedTextLineBoxes: [{ x0: 0.05, y0: 0.05, x1: 0.95, y1: 0.3 }],
      protectedBoxes: [],
    },
  );
  assert.equal(graphicResult.evidence, "complex", JSON.stringify(graphicResult));
  assert.equal(graphicResult.reason, "high_contrast_graphic");
});

test("protected geometry overlapping a trusted text region fails closed", () => {
  const result = pdfBackground.sampleCanvasRegionBackground(
    { width: 100, height: 50 },
    { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.9 },
    {
      trustedTextLineBoxes: [{ x0: 0.15, y0: 0.2, x1: 0.85, y1: 0.35 }],
      protectedBoxes: [{ x0: 0.4, y0: 0.25, x1: 0.6, y1: 0.5 }],
    },
  );

  assert.equal(result.evidence, "complex");
  assert.equal(result.reason, "protected_geometry_overlap");
  assert.equal(result.backgroundColor, null);
});

test("chooses a readable light foreground for a dark uniform background", () => {
  const result = pdfBackground.classifyPdfBackgroundPixels(
    pixels(8, 8, () => [27, 31, 38, 255]),
  );

  assert.equal(result.evidence, "uniform");
  assert.equal(result.foregroundColor, "rgb(248, 249, 251)");
  assert.ok(result.contrastRatio >= 4.5);
});

test("fails closed when any sampled pixel is transparent", () => {
  const result = pdfBackground.classifyPdfBackgroundPixels(
    pixels(8, 8, (x, y) => (x === 0 && y === 0 ? [255, 255, 255, 254] : [255, 255, 255, 255])),
  );

  assert.equal(result.evidence, "complex");
  assert.equal(result.reason, "transparent_pixels");
  assert.equal(result.backgroundColor, null);
  assert.equal(result.foregroundColor, null);
});

test("fails closed for checkerboard and spatially localized backgrounds", () => {
  const checkerboard = pdfBackground.classifyPdfBackgroundPixels(
    pixels(12, 12, (x, y) => ((x + y) % 2 ? [245, 245, 245, 255] : [80, 100, 130, 255])),
  );
  assert.equal(checkerboard.evidence, "complex");
  assert.equal(checkerboard.reason, "dominant_color_low");
  assert.equal(checkerboard.backgroundColor, null);

  const localized = pdfBackground.classifyPdfBackgroundPixels(
    pixels(12, 12, (x) => (x < 3 ? [190, 210, 235, 255] : [250, 250, 250, 255])),
  );
  assert.equal(localized.evidence, "complex");
  assert.equal(localized.reason, "spatial_nonuniform");
  assert.equal(localized.backgroundColor, null);

  const repeatedShading = pdfBackground.classifyPdfBackgroundPixels(
    pixels(12, 12, (_x, y) => (y % 4 === 0 ? [210, 210, 210, 255] : [250, 250, 250, 255])),
  );
  assert.equal(repeatedShading.evidence, "complex");
  assert.equal(repeatedShading.reason, "spatial_nonuniform");
  assert.equal(repeatedShading.backgroundColor, null);
});

test("rejects malformed or undersized buffers as unknown", () => {
  const malformed = pdfBackground.classifyPdfBackgroundPixels({
    data: new Uint8ClampedArray(3),
    width: 1,
    height: 1,
  });
  assert.equal(malformed.evidence, "unknown");
  assert.equal(malformed.reason, "invalid_pixel_buffer");

  const small = pdfBackground.classifyPdfBackgroundPixels(
    pixels(2, 2, () => [255, 255, 255, 255]),
  );
  assert.equal(small.evidence, "unknown");
  assert.equal(small.reason, "insufficient_samples");
});

test("samples normalized canvas regions without smoothing", () => {
  const sampled = pixels(8, 8, () => [244, 245, 247, 255]);
  const drawCalls = [];
  const context = {
    imageSmoothingEnabled: true,
    clearRect() {},
    drawImage(...args) {
      drawCalls.push(args);
    },
    getImageData() {
      return sampled;
    },
  };
  const ownerDocument = {
    createElement(tag) {
      assert.equal(tag, "canvas");
      return {
        width: 0,
        height: 0,
        getContext(type, options) {
          assert.equal(type, "2d");
          assert.deepEqual(options, { willReadFrequently: true });
          return context;
        },
      };
    },
  };
  const canvas = { width: 200, height: 100, ownerDocument };
  const result = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0.25, y0: 0.2, x1: 0.75, y1: 0.6 },
    { maximumSampleDimension: 8 },
  );

  assert.equal(result.evidence, "uniform");
  assert.equal(context.imageSmoothingEnabled, false);
  assert.equal(drawCalls.length, 1);
  assert.deepEqual(drawCalls[0].slice(1, 5), [50, 20, 100, 40]);
  assert.deepEqual(drawCalls[0].slice(5), [0, 0, 8, 3]);
});

test("canvas sampling returns no colors for invalid geometry or read failures", () => {
  const ownerDocument = {
    createElement() {
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            clearRect() {},
            drawImage() {
              throw new Error("tainted canvas");
            },
            getImageData() {
              throw new Error("unreachable");
            },
          };
        },
      };
    },
  };
  const canvas = { width: 100, height: 100, ownerDocument };

  const invalid = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: -0.1, y0: 0, x1: 0.5, y1: 0.5 },
  );
  assert.equal(invalid.reason, "invalid_region");
  assert.equal(invalid.backgroundColor, null);

  const failed = pdfBackground.sampleCanvasRegionBackground(
    canvas,
    { x0: 0, y0: 0, x1: 1, y1: 1 },
  );
  assert.equal(failed.evidence, "unknown");
  assert.equal(failed.reason, "canvas_read_failed");
  assert.equal(failed.foregroundColor, null);
});
