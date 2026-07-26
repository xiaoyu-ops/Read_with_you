import type { TextMeasureInput, TranslationTextMeasurer } from "./translationFit";

export type DisposableTranslationTextMeasurer = TranslationTextMeasurer & {
  dispose(): void;
};

/** Browser-only CSS text measurement used by the future inline PDF overlay. */
export function createDomTranslationMeasurer(
  ownerDocument: Document = document,
): DisposableTranslationTextMeasurer {
  // Keep this factory self-contained so the browser regression can execute the
  // exact production implementation inside a real page.
  const verificationScales = [1, 1.5, 2] as const;
  const overflowTolerancePx = 1;
  const join = (tokens: readonly { value: string }[]) =>
    tokens.map((token) => token.value).join("");
  const node = ownerDocument.createElement("div");
  node.setAttribute("aria-hidden", "true");
  Object.assign(node.style, {
    position: "fixed",
    left: "-100000px",
    top: "0",
    zIndex: "-1",
    boxSizing: "border-box",
    margin: "0",
    padding: "0",
    border: "0",
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    wordBreak: "normal",
    overflow: "hidden",
    visibility: "hidden",
    fontFamily: '"Noto Serif SC", "Songti SC", "STSong", serif',
    fontWeight: "400",
    fontStyle: "normal",
    letterSpacing: "0",
  });
  ownerDocument.body.appendChild(node);

  const fitsAtScale = (input: TextMeasureInput, scale: number): boolean => {
    node.style.width = `${input.widthPx100 * scale}px`;
    node.style.height = `${input.heightPx100 * scale}px`;
    node.style.fontSize = `${input.fontPx100 * scale}px`;
    node.style.lineHeight = `${input.lineHeightPx100 * scale}px`;
    node.textContent = join(input.tokens);
    return (
      node.scrollWidth <= node.clientWidth + overflowTolerancePx &&
      node.scrollHeight <= node.clientHeight + overflowTolerancePx
    );
  };
  const fits = (input: TextMeasureInput): boolean =>
    verificationScales.every((scale) => fitsAtScale(input, scale));

  return {
    maxFittingPrefix(input) {
      let lower = 0;
      let upper = input.tokens.length;
      while (lower < upper) {
        const middle = Math.ceil((lower + upper) / 2);
        if (fits({ ...input, tokens: input.tokens.slice(0, middle) })) lower = middle;
        else upper = middle - 1;
      }
      return lower;
    },
    verify: fits,
    dispose() {
      node.remove();
    },
  };
}
