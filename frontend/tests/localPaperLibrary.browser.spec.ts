import { createHash } from "node:crypto";

import { expect, test, type Page, type Route } from "playwright/test";

const PAPER_ID = "2202.09741";
const pdf = Buffer.from("%PDF-local-library");
const upperAsset = Buffer.from("UPPER-ASSET");
const lowerAsset = Buffer.from("lower-asset");
const document = Buffer.from(
  JSON.stringify({
    paper_id: PAPER_ID,
    title: "Visual Attention Network",
    source: "arxiv",
    extracted_at: "2026-07-24T00:00:00Z",
    blocks: [],
  }),
);

function digest(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function multipartBundle() {
  const boundary = "peinidu-browser-test";
  const files = [
    { path: "paper/assets/ModalNet-19.png", body: upperAsset },
    { path: "paper/assets/anaphora.png", body: lowerAsset },
    { path: "paper/original.pdf", body: pdf },
    { path: "paper/translation.json", body: document },
  ];
  const entries = files.map((item) => ({
    path: item.path,
    size: item.body.byteLength,
    sha256: digest(item.body),
  }));
  const manifest = {
    version: 1,
    paper_id: PAPER_ID,
    paper: {
      title: "Visual Attention Network",
      authors: ["Meng-Hao Guo"],
      source: "arxiv",
      status: "translated",
    },
    revision: "a".repeat(64),
    base_revision: null,
    bundle_type: "full",
    files: entries,
    included_paths: entries.map((entry) => entry.path),
    deleted_paths: [],
    total_bytes: entries.reduce((total, entry) => total + entry.size, 0),
  };
  const chunks: Buffer[] = [];
  chunks.push(
    Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="manifest"\r\nContent-Type: application/json\r\n\r\n${JSON.stringify(manifest)}\r\n`,
    ),
  );
  files.forEach((item, index) => {
    chunks.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${index}.bin"\r\nContent-Type: application/octet-stream\r\n\r\n`,
      ),
      item.body,
      Buffer.from("\r\n"),
    );
  });
  chunks.push(Buffer.from(`--${boundary}--\r\n`));
  return {
    body: Buffer.concat(chunks),
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}

async function mockApi(page: Page) {
  let acknowledgements = 0;
  let collectionCreates = 0;
  let restorePayloadOrderCorrect = false;
  await page.route("**/api-mock/**", async (route: Route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (
      request.method() === "GET" &&
      pathname === `/api-mock/papers/${PAPER_ID}/portable-bundle`
    ) {
      const bundle = multipartBundle();
      await route.fulfill({
        status: 200,
        contentType: bundle.contentType,
        body: bundle.body,
      });
      return;
    }
    if (request.method() === "GET" && pathname === "/api-mock/papers") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            arxiv_id: PAPER_ID,
            title: "Visual Attention Network",
            authors: ["Meng-Hao Guo"],
            source: "arxiv",
            status: "translated",
          },
        ]),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api-mock/papers/portable-bundle") {
      const payload = request.postDataBuffer()?.toString("utf8") ?? "";
      const upperPosition = payload.indexOf(upperAsset.toString());
      const lowerPosition = payload.indexOf(lowerAsset.toString());
      restorePayloadOrderCorrect =
        upperPosition >= 0 && lowerPosition > upperPosition;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          arxiv_id: PAPER_ID,
          revision: "a".repeat(64),
          status: "restored",
        }),
      });
      return;
    }
    if (
      request.method() === "POST" &&
      pathname === `/api-mock/papers/${PAPER_ID}/portable-bundle/ack`
    ) {
      acknowledgements += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          arxiv_id: PAPER_ID,
          revision: "a".repeat(64),
          cache_acknowledged: true,
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === "/api-mock/config") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          llm_providers: [],
          task_models: {},
          prompts: {},
          mcp_servers: [],
          mineru: {},
          deeplx: { base_url: "", api_key: "", timeout_seconds: 30 },
          translation_prompt: "",
          translation_concurrency: 1,
          request_timeout: 30,
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === "/api-mock/collections") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { id: 1, name: "视觉注意力", paper_count: 1, contains_paper: true },
          { id: 2, name: "待整理", paper_count: 0, contains_paper: false },
        ]),
      });
      return;
    }
    if (request.method() === "GET" && pathname === "/api-mock/collections/1") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1,
          name: "视觉注意力",
          papers: [{ arxiv_id: PAPER_ID, title: "Visual Attention Network" }],
        }),
      });
      return;
    }
    if (request.method() === "GET" && pathname === "/api-mock/collections/2") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 2,
          name: "待整理",
          papers: [],
        }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api-mock/collections") {
      collectionCreates += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1,
          name: "视觉注意力",
          paper_count: 1,
          contains_paper: true,
        }),
      });
      return;
    }
    if (request.method() === "POST" && pathname === "/api-mock/collections/1/papers") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1,
          name: "视觉注意力",
          papers: [{ arxiv_id: PAPER_ID, title: "Visual Attention Network" }],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "not mocked" }),
    });
  });
  return {
    acknowledgements: () => acknowledgements,
    collectionCreates: () => collectionCreates,
    restorePayloadOrderCorrect: () => restorePayloadOrderCorrect,
  };
}

test("user selects a folder and sees saved only after bytes are present", async ({ page }) => {
  const api = await mockApi(page);
  await page.addInitScript(async (paperId) => {
    const root = await navigator.storage.getDirectory();
    Object.defineProperty(window, "showDirectoryPicker", {
      configurable: true,
      value: async () => root,
    });
    window.localStorage.setItem(
      "peinidu.currentReading",
      JSON.stringify({
        arxiv_id: paperId,
        title: "Visual Attention Network",
        authors: ["Meng-Hao Guo"],
        source: "arxiv",
        block_count: 1,
        updated_at: new Date().toISOString(),
      }),
    );
  }, PAPER_ID);

  await page.goto("/config");
  await expect(page.getByRole("heading", { name: "论文保存位置" })).toBeVisible();
  await page.getByRole("button", { name: "选择本地文件夹" }).click();
  await expect(page.getByText("本地文献库已启用")).toBeVisible();
  await page.getByRole("button", { name: "同步当前论文" }).click();
  await expect(page.getByText(/已完整写入并通过哈希校验/)).toBeVisible();

  const saved = await page.evaluate(async (paperId) => {
    const root = await navigator.storage.getDirectory();
    const paper = await root.getDirectoryHandle(encodeURIComponent(paperId));
    const pdfHandle = await (
      await paper.getDirectoryHandle("paper")
    ).getFileHandle("original.pdf");
    const manifest = JSON.parse(
      await (await paper.getFileHandle("manifest.json")).getFile().then((file) => file.text()),
    );
    return {
      pdf: await (await pdfHandle.getFile()).text(),
      revision: manifest.revision,
    };
  }, PAPER_ID);
  expect(saved.pdf).toBe("%PDF-local-library");
  expect(saved.revision).toBe("a".repeat(64));
  expect(api.acknowledgements()).toBe(1);

  const migrationPaper = page.getByRole("checkbox", {
    name: /Visual Attention Network/,
  });
  await expect(migrationPaper).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "保存所选论文到此目录" }),
  ).toBeDisabled();
  await migrationPaper.check();
  await page.getByRole("button", { name: "保存所选论文到此目录" }).click();
  await expect(
    page.getByText(/已将所选 1 篇论文和 1 个相关专题完整写入/),
  ).toBeVisible();
  const workspace = await page.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    return JSON.parse(
      await (await root.getFileHandle("peinidu-workspace.json"))
        .getFile()
        .then((file) => file.text()),
    );
  });
  expect(workspace.papers).toEqual([
    { paper_id: PAPER_ID, revision: "a".repeat(64) },
  ]);
  expect(workspace.collections).toEqual([
    { name: "视觉注意力", paper_ids: [PAPER_ID] },
  ]);

  await page.getByRole("button", { name: "从此目录恢复" }).click();
  await expect(page.getByText("已从本地目录恢复 1 篇论文及其专题。")).toBeVisible();
  expect(api.acknowledgements()).toBeGreaterThanOrEqual(3);
  expect(api.collectionCreates()).toBe(0);
  expect(api.restorePayloadOrderCorrect()).toBe(true);
});

test("canceling folder selection keeps the current local storage choice", async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(() => {
    Object.defineProperty(window, "showDirectoryPicker", {
      configurable: true,
      value: async () => {
        throw new DOMException("The user aborted a request.", "AbortError");
      },
    });
  });

  await page.goto("/config");
  await page.getByRole("button", { name: "选择本地文件夹" }).click();

  await expect(page.getByText("未选择文件夹，保存位置没有改变。")).toBeVisible();
  await expect(page.getByText("这台电脑", { exact: true })).toBeVisible();
  await expect(page.getByText("Failed to execute 'showDirectoryPicker'")).toHaveCount(0);
});
