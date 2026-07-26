import { expect, test, type Page, type Route } from "playwright/test";

const ORIGIN = "a".repeat(40);
const RELATED = ["b", "c", "d", "e", "f"].map((value) => value.repeat(40));

function paper(
  id: string,
  title: string,
  year: number,
  citationCount: number,
  arxivId: string | null = null,
) {
  return {
    id,
    arxiv_id: arxivId,
    doi: null,
    title,
    authors: [`${title.split(" ")[0]} Researcher`, "Second Author"],
    abstract: `${title} studies a bounded research question with verifiable evidence.`,
    year,
    venue: "Research Conference",
    citation_count: citationCount,
    reference_count: 24,
    is_open_access: Boolean(arxivId),
    pdf_url: arxivId ? `https://example.test/${arxivId}.pdf` : null,
    url: `https://www.semanticscholar.org/paper/${id}`,
    similarity: id === ORIGIN ? 1 : 0.9,
    role: id === ORIGIN ? "origin" : "related",
  };
}

const nodes = [
  paper(ORIGIN, "Core Graph Paper", 2020, 3200, "2001.00001"),
  paper(RELATED[0], "Earlier Representation Study", 2016, 1400),
  paper(RELATED[1], "Semantic Embedding Analysis", 2019, 700, "1901.00002"),
  paper(RELATED[2], "Directed Citation Networks", 2021, 320),
  paper(RELATED[3], "Recent Literature Discovery", 2024, 80, "2401.00003"),
  paper(RELATED[4], "Open Research Mapping", 2025, 20),
];

const mapPayload = {
  version: 1,
  origin: nodes[0],
  nodes,
  edges: [
    { source: ORIGIN, target: RELATED[0], kind: "similarity", weight: 0.92, provenance: "semantic_scholar_specter2" },
    { source: ORIGIN, target: RELATED[1], kind: "similarity", weight: 0.87, provenance: "semantic_scholar_specter2" },
    { source: RELATED[1], target: RELATED[3], kind: "similarity", weight: 0.8, provenance: "semantic_scholar_specter2" },
    { source: ORIGIN, target: RELATED[0], kind: "citation", weight: 1, provenance: "semantic_scholar_academic_graph" },
    { source: RELATED[3], target: ORIGIN, kind: "citation", weight: 1, provenance: "semantic_scholar_academic_graph" },
  ],
  prior_works: [
    { paper: nodes[1], graph_citation_count: 4 },
  ],
  derivative_works: [
    { paper: nodes[4], graph_reference_count: 3 },
  ],
  status: "complete",
  provider: "semantic_scholar",
  retrieved_at: "2026-07-26T00:00:00Z",
  cached: false,
  stale: false,
  warnings: [],
};

async function mockApi(page: Page) {
  await page.route("**/api-mock/**", async (route: Route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const json = (payload: unknown) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
    if (pathname.startsWith("/api-mock/literature-map/")) return json(mapPayload);
    if (pathname === "/api-mock/papers") return json([]);
    if (pathname === "/api-mock/search") {
      return json({
        candidates: [
          {
            arxiv_id: "2001.00001",
            paper_id: ORIGIN,
            title: "Core Graph Paper",
            authors: ["Core Researcher"],
            abstract: "Core paper abstract",
            year: "2020",
            url: "https://arxiv.org/abs/2001.00001",
            source: "merged",
            extractable: true,
          },
          {
            arxiv_id: "",
            paper_id: RELATED[0],
            title: "S2 Only Paper",
            authors: ["S2 Researcher"],
            abstract: "S2-only abstract",
            year: "2016",
            url: "https://www.semanticscholar.org",
            source: "s2",
            extractable: false,
          },
        ],
      });
    }
    return route.fulfill({ status: 404, body: "not mocked" });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

test("homepage keeps map task context and preserves S2-only graph access", async ({ page }) => {
  await mockApi(page);
  await page.goto("/?task=map");
  await expect(page.getByRole("tab", { name: "看论文关系" })).toHaveAttribute("aria-selected", "true");
  await page.getByPlaceholder("输入一篇核心论文的标题 / arXiv ID / URL").fill("graph paper");
  await page.getByRole("button", { name: "检索", exact: true }).click();
  const cards = page.locator("article");
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(1).getByRole("button", { name: "无可提取版本" })).toBeDisabled();
  await expect(cards.nth(1).getByRole("button", { name: "查看图谱" })).toBeEnabled();
  await expectNoHorizontalOverflow(page);
});

test("graph workspace synchronizes views and stays non-overlapping at target widths", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto(`/literature-map/${ORIGIN}`);
  await expect(page.getByRole("heading", { name: "Core Graph Paper" })).toBeVisible();
  await expect(page.locator(".literature-node")).toHaveCount(nodes.length);

  const desktopColumns = await page.locator(".literature-map-grid").evaluate((element) =>
    getComputedStyle(element).gridTemplateColumns,
  );
  expect(desktopColumns).toMatch(/^300px .* 340px$/);

  await page.getByRole("button", { name: "引用关系", exact: true }).click();
  await expect(page.locator(".literature-edge-citation")).toHaveCount(2);
  await page.getByRole("button", { name: "选择论文：Recent Literature Discovery" }).press("Enter");
  await expect(page.getByRole("heading", { name: "Recent Literature Discovery" })).toBeVisible();
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await page.getByPlaceholder("标题、作者或会议").fill("Embedding");
  await expect(page.locator(".literature-node")).toHaveCount(2);
  await page.getByRole("button", { name: "清除筛选" }).click();
  await page.getByRole("button", { name: "先行工作" }).click();
  await expect(page.getByText("被图中多篇论文共同引用的工作")).toBeVisible();
  await expect(page.locator(".literature-table-view tbody tr")).toHaveCount(1);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(page.locator(".literature-map-list")).toBeHidden();
  await expect(page.locator(".literature-map-detail")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".literature-mobile-tabs")).toBeVisible();
  await page.getByRole("button", { name: "当前论文", exact: true }).click();
  await expect(page.locator(".literature-map-detail")).toBeVisible();
  await expect(page.locator(".literature-map-stage")).toBeHidden();
  await expectNoHorizontalOverflow(page);
});
