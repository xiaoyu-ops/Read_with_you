import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeSelectionText,
  scoreGoldSelections,
} from "../../scripts/audit_pdf_text_selection.mjs";


test("normalizes PDF line breaks without changing selected characters", () => {
  assert.equal(
    normalizeSelectionText("The dominant\n sequence\tmodel"),
    "The dominant sequence model",
  );
});

test("selection audit fails closed instead of accepting fuzzy text", () => {
  const exact = scoreGoldSelections("Alpha beta gamma", ["Alpha beta"]);
  assert.equal(exact.precision, 1);
  assert.equal(exact.recall, 1);
  assert.equal(exact.selections[0].exact, true);

  const changed = scoreGoldSelections("Alpha better gamma", ["Alpha beta"]);
  assert.equal(changed.precision, 0);
  assert.equal(changed.recall, 0);
  assert.equal(changed.selections[0].exact, false);
});
