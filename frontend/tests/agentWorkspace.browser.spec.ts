import { expect, test, type Page, type Route } from "playwright/test";

const PAPER_ID = "2202.09741";
const PATH = `/agent/${PAPER_ID}`;
const now = "2026-07-23T08:00:00Z";

const paper = {
  arxiv_id: PAPER_ID,
  title: "Visual Attention Network",
  authors: ["Meng-Hao Guo", "Cheng-Ze Lu"],
  source: "arxiv",
  status: "ready",
  blocks: [
    {
      index: 11,
      type: "paragraph",
      original: "The model uses large kernel attention.",
      translation: null,
      status: "pending",
    },
  ],
};

const initialMessages = [
  {
    id: "message-user-1",
    role: "user",
    content: "这个方法的关键是什么？",
    created_at: now,
  },
  {
    id: "message-assistant-1",
    role: "assistant",
    content: "关键是以大核注意力建立长距离关系，同时保留卷积归纳偏置。",
    created_at: now,
    meta: {
      client_context: {
        reader: {
          page: 2,
          selected_text: { text: "large kernel attention" },
        },
      },
      result_data: {
        summary: "大核注意力",
        evidence: [
          {
            claim: "大核注意力是核心方法。",
            location: {
              arxiv_id: PAPER_ID,
              page: 2,
              block_index: 11,
              region_id: "region-11",
            },
          },
        ],
        limits: [],
        next_questions: [],
        actions: [
          {
            kind: "open_literature_map",
            label: "打开论文图谱",
            href: `/literature-map/ARXIV%3A${PAPER_ID}`,
          },
        ],
      },
    },
  },
];

function chatState(messages = initialMessages) {
  return {
    arxiv_id: PAPER_ID,
    messages,
    memories: [],
    skills: [],
    runs: [],
  };
}

async function mockAgentApi(page: Page) {
  const messagePayloads: Array<Record<string, unknown>> = [];
  let clearRequests = 0;
  await page.route("**/api-mock/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const json = (payload: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(payload),
      });

    if (
      request.method() === "POST" &&
      pathname === `/api-mock/agent/chat/${PAPER_ID}/messages/stream`
    ) {
      messagePayloads.push(request.postDataJSON() as Record<string, unknown>);
      const nextMessages = [
        ...initialMessages,
        {
          id: "message-user-2",
          role: "user",
          content: "补充一句",
          created_at: now,
        },
        {
          id: "message-assistant-2",
          role: "assistant",
          content: "补充结论已写入当前对话。",
          created_at: now,
          meta: initialMessages[1].meta,
        },
      ];
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: [
          'event: agent_event\ndata: {"message":"正在核对证据"}',
          'event: delta\ndata: {"text":"补充结论"}',
          `event: done\ndata: ${JSON.stringify({ state: chatState(nextMessages) })}`,
          "",
        ].join("\n\n"),
      });
    }
    if (
      request.method() === "DELETE" &&
      pathname === `/api-mock/agent/chat/${PAPER_ID}`
    ) {
      clearRequests += 1;
      return json(chatState([]));
    }
    if (pathname === `/api-mock/agent/chat/${PAPER_ID}`) return json(chatState());
    if (pathname === "/api-mock/agent/chats") {
      return json([
        {
          arxiv_id: PAPER_ID,
          paper_title: paper.title,
          paper_exists: true,
          message_count: 2,
          last_role: "assistant",
          last_message: initialMessages[1].content,
          updated_at: now,
        },
      ]);
    }
    if (pathname === `/api-mock/papers/${PAPER_ID}/annotations`) {
      return json([
        {
          id: "annotation-1",
          arxiv_id: PAPER_ID,
          block_index: 11,
          side: "original",
          text: "large kernel attention",
          note: "检查大核是否带来更高计算量。",
          color: "#d7e6f5",
          kind: "question",
          created_at: now,
          updated_at: now,
          selector: {
            version: 2,
            source_pdf_sha256: "a".repeat(64),
            page: 2,
            start: { item_index: 1, char_offset: 0 },
            end: { item_index: 1, char_offset: 22 },
            quote: { exact: "large kernel attention", prefix: "", suffix: "" },
            rects: [{ x0: 0.1, y0: 0.2, x1: 0.4, y1: 0.24 }],
            region_id: "region-11",
            layout_confidence: 0.98,
          },
        },
      ]);
    }
    if (pathname === `/api-mock/papers/${PAPER_ID}/paper-note`) {
      return json({
        arxiv_id: PAPER_ID,
        markdown: "# 阅读笔记\n\n重点比较大核注意力与自注意力。",
        updated_at: now,
        revision: "b".repeat(64),
      });
    }
    if (pathname === `/api-mock/papers/${PAPER_ID}`) return json(paper);
    if (pathname === "/api-mock/papers") return json([paper]);
    return route.fulfill({ status: 404, body: "not mocked" });
  });
  return { messagePayloads, clearRequests: () => clearRequests };
}

async function assertNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

test("research workspace streams conversation and adapts at four target widths", async ({
  page,
}) => {
  const api = await mockAgentApi(page);
  await page.addInitScript(
    ({ paperId, createdAt }) => {
      window.sessionStorage.setItem(
        "pet:agent-workspace-handoff",
        JSON.stringify({
          version: 1,
          arxiv_id: paperId,
          created_at: createdAt,
          reader: {
            reader_mode: "selection_translation",
            active_block: {
              index: 11,
              type: "paragraph",
              original: "The model uses large kernel attention.",
              translation: null,
              status: "pending",
            },
            previous_block: null,
            next_block: null,
            selected_text: {
              block_index: 11,
              side: "original",
              text: "large kernel attention",
            },
            page: 2,
            region_id: "region-11",
            layout_confidence: 0.98,
            render_policy: "preserve",
          },
        }),
      );
    },
    { paperId: PAPER_ID, createdAt: Date.now() },
  );
  await page.goto(PATH);

  await expect(page.getByRole("heading", { name: "研究线索" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "证据与上下文" })).toBeVisible();
  await expect(page.getByText("选区：large kernel attention")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "大核注意力是核心方法。" }).first(),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "打开论文图谱" })).toHaveAttribute(
    "href",
    `/literature-map/ARXIV%3A${PAPER_ID}`,
  );
  await assertNoHorizontalOverflow(page);

  const leftSeparator = page.getByRole("separator", { name: "调整会话栏宽度" });
  await leftSeparator.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("peinidu.agent.left-width")))
    .toBe("288");

  await page.getByLabel("向 Agent 提问").fill("补充一句");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("补充结论已写入当前对话。")).toBeVisible();
  expect(api.messagePayloads).toHaveLength(1);
  expect(
    (
      (api.messagePayloads[0].context as Record<string, unknown>)
        .reader as Record<string, unknown>
    ).region_id,
  ).toBe("region-11");
  await expect
    .poll(() =>
      page.evaluate(() => sessionStorage.getItem("pet:agent-workspace-handoff")),
    )
    .toBeNull();

  for (const width of [1066, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await assertNoHorizontalOverflow(page);
    await page.getByRole("button", { name: /会话栏/ }).click();
    await expect(page.getByRole("heading", { name: "研究线索" })).toBeVisible();
    await page.getByRole("button", { name: "关闭会话列表" }).click();
  }

  await page.setViewportSize({ width: 360, height: 780 });
  await assertNoHorizontalOverflow(page);
  await page.getByRole("button", { name: "收起资料栏", exact: true }).click();
  await expect(page.getByRole("heading", { name: "证据与上下文" })).toBeVisible();
  const evidenceLink = page
    .locator(".agent-workspace-inspector")
    .getByRole("button", { name: "大核注意力是核心方法。" });
  await Promise.all([
    page.waitForURL(`/paper/${PAPER_ID}`),
    evidenceLink.click(),
  ]);
  await expect
    .poll(() =>
      page.evaluate(() => sessionStorage.getItem("pet:pending-reader-evidence")),
    )
    .toBeNull();
});

test("conversation menu requires a second confirmation before clearing", async ({ page }) => {
  const api = await mockAgentApi(page);
  await page.goto(PATH);

  await page.getByLabel("会话菜单").click();
  await page.getByRole("button", { name: "清空当前会话" }).click();
  expect(api.clearRequests()).toBe(0);
  await expect(page.getByText("这会取消当前运行并删除这篇论文的对话记录。")).toBeVisible();
  await page.getByRole("button", { name: "确认清空" }).click();
  await expect(page.getByText("从一个具体问题开始。")).toBeVisible();
  expect(api.clearRequests()).toBe(1);
});
