#!/usr/bin/env node
// Pet 入口体验回归：素材加载 / 状态机 / 权限确认 / 选区解释 / 长消息 / 窄屏。
// 复用运行中的前后端（默认 3000/8000），不自行拉起服务；产生的对话数据由调用方负责清理。
// 用法：node scripts/verify_pet_flow.mjs [--app-base URL] [--api-base URL]

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const rootDir = path.resolve(path.dirname(scriptPath), "..");
const requireFromFrontend = createRequire(path.join(rootDir, "frontend", "package.json"));
const { chromium } = requireFromFrontend("playwright");

const getArg = (name, fallback) => {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
};
const appBase = getArg("--app-base", "http://127.0.0.1:3000");
const apiBase = getArg("--api-base", "http://127.0.0.1:8000");
const arxivId = getArg("--arxiv-id", "1706.03762");

const outDir = path.join(rootDir, "output", "playwright");
fs.mkdirSync(outDir, { recursive: true });
const shot = (name) => path.join(outDir, `pet-${name}.png`);

const steps = [];
const record = (name, ok, detail = "") => {
  steps.push({ name, ok, detail });
  console.log(`${ok ? "✅" : "❌"} ${name}${detail ? ` — ${detail}` : ""}`);
};

const consoleErrors = [];
const pageErrors = [];

const CHAT_INPUT = 'input[placeholder="问当前论文，或让子 Agent 做任务"]';
const SPRITE = ".pet-sprite";
const PET_BUTTON = ".pet-assistant-button";

async function spriteStatus(page) {
  return page.$eval(SPRITE, (el) => el.dataset.status);
}

async function sendChat(page, text) {
  await page.fill(CHAT_INPUT, text);
  await page.click('form button:has-text("发送")');
}

async function main() {
  // 与 simulate_user_flow.mjs 一致：playwright 自带浏览器缺失时回退系统 Chrome
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch {
    browser = await chromium.launch({
      headless: true,
      executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
  }
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));

  try {
    // 1. 素材经 Next.js 静态可访问
    for (const pose of ["idle", "talking", "thinking", "working", "confirm"]) {
      const resp = await page.request.get(`${appBase}/pet/${pose}.png`);
      if (!resp.ok()) throw new Error(`/pet/${pose}.png -> ${resp.status()}`);
    }
    record("pet 五张状态图静态访问 200", true);

    // 2. 打开阅读页，sprite 挂载且图片全部加载
    await page.goto(`${appBase}/paper/${arxivId}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(SPRITE, { timeout: 30000 });
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll(".pet-sprite img")).every(
        (img) => img.complete && img.naturalWidth > 0,
      ),
      undefined,
      { timeout: 15000 },
    );
    const poses = await page.$$eval(".pet-sprite img", (els) => els.map((e) => e.dataset.pose));
    record("sprite 挂载且 5 张姿势图加载成功", poses.length === 5, poses.join(","));
    record("初始状态为 idle", (await spriteStatus(page)) === "idle");
    await page.locator(PET_BUTTON).screenshot({ path: shot("idle") });

    // 3. 打开聊天窗口；打开动作本身不应触发口型（首次加载历史不说话）
    await page.click(PET_BUTTON);
    await page.waitForSelector('h2:has-text("阅读 Pet")', { timeout: 10000 });
    await page.waitForFunction(
      () => !document.body.textContent.includes("正在进入当前论文上下文"),
      undefined,
      { timeout: 15000 },
    );
    await page.waitForTimeout(300);
    const statusAfterOpen = await spriteStatus(page);
    record("打开聊天后状态不误报 talking", statusAfterOpen === "idle", `status=${statusAfterOpen}`);
    await page.screenshot({ path: shot("chat-open") });

    // 4. 权限确认流：外部检索 -> confirm 状态 -> 确认 -> 后台任务落地
    await sendChat(page, "帮我联网检索这篇论文的相关工作");
    await page.waitForSelector('button:has-text("确认外部检索")', { timeout: 15000 });
    const confirmStatus = await spriteStatus(page);
    record("权限确认卡片出现且 Pet 进入 confirm", confirmStatus === "confirm", `status=${confirmStatus}`);
    await page.locator(PET_BUTTON).screenshot({ path: shot("confirm") });
    await page.screenshot({ path: shot("confirm-card") });
    await page.click('button:has-text("确认外部检索")');
    // 真实外部检索 + LLM 汇总成功时回填 "外部工具请求完成：{LLM 摘要}"；
    // 汇总失败的兜底文案才含 "已确认权限"。两者都算通过；真实网络给足预算。
    await page.waitForFunction(
      () =>
        document.body.textContent.includes("外部工具请求完成") ||
        document.body.textContent.includes("已确认权限"),
      undefined,
      { timeout: 45000 },
    );
    record("确认后创建 external_tool_request 并回填结果", true);

    // 5. 真实鼠标拖选段落 -> 重开聊天 -> Pet 头部显示选区上下文 -> 提问触发 selection_explanation
    await page.click('button:has-text("关闭")'); // 先关聊天，避免窗口遮挡拖选目标
    await page.waitForTimeout(200);
    const target = page
      .locator("div.reader-pane p.block-row")
      .filter({ hasText: /.{80,}/ })
      .first();
    await target.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const box = await target.boundingBox();
    if (!box) throw new Error("没有找到可选中的段落 block");
    const startX = box.x + 4;
    const startY = box.y + Math.min(12, box.height / 2);
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(startX + Math.min(box.width - 12, 260), startY, { steps: 10 });
    await page.mouse.up();
    const selectedText = await page.evaluate(() => window.getSelection()?.toString() ?? "");
    if (selectedText.trim().length < 2) throw new Error("拖选没有产生选区");
    await page.click(PET_BUTTON); // 重开聊天
    await page.waitForFunction(
      () => document.body.textContent.includes("选区 #"),
      undefined,
      { timeout: 8000 },
    );
    record("选中文本后 Pet 显示选区上下文", true, `选区: ${selectedText.slice(0, 24)}…`);

    await sendChat(page, "这段是什么意思？");
    await page.waitForSelector('section :text("选区解释")', { timeout: 15000 });
    // 状态是瞬时值：发送态翻转与 run 列表渲染可能分两次 commit，
    // 轮询等待进入 working；run 执行太快直接完成也算通过
    let workingStatus = await spriteStatus(page);
    for (let i = 0; i < 20 && workingStatus !== "working"; i += 1) {
      const finished = await page.evaluate(() =>
        document.body.textContent.includes("选区解释完成"),
      );
      if (finished) break;
      await page.waitForTimeout(250);
      workingStatus = await spriteStatus(page);
    }
    const runObserved =
      workingStatus === "working" ||
      (await page.evaluate(() => document.body.textContent.includes("选区解释完成")));
    record(
      "选区解释 Run 创建，Pet 进入 working",
      runObserved,
      `status=${workingStatus}`,
    );
    await page.locator(PET_BUTTON).screenshot({ path: shot("working") });

    await page.waitForFunction(
      () => document.body.textContent.includes("选区解释完成"),
      undefined,
      { timeout: 120000 },
    );
    record("选区解释 Run 完成并回填聊天", true);
    let sawTalking = false;
    try {
      await page.waitForFunction(
        () => document.querySelector(".pet-sprite")?.dataset.status === "talking",
        undefined,
        { timeout: 8000 },
      );
      sawTalking = true;
      await page.locator(PET_BUTTON).screenshot({ path: shot("talking") });
    } catch {
      // 口型窗口只有 2.4s，偶发错过不视为失败，但要记录
    }
    record("回复到达后出现 talking 口型窗口", sawTalking, sawTalking ? "" : "未在窗口期捕获");

    // 6. 后端确实收到完整 reader 上下文
    const chatResp = await page.request.get(`${apiBase}/agent/chat/${arxivId}`);
    const chatData = await chatResp.json();
    const userMessages = chatData.messages.filter(
      (m) => m.role === "user" && m.content === "这段是什么意思？",
    );
    const reader = userMessages.at(-1)?.meta?.client_context?.reader;
    const contextOk = Boolean(
      reader?.selected_text?.text &&
      reader?.active_block &&
      reader?.previous_block &&
      reader?.next_block,
    );
    record(
      "请求携带 selected/active/previous/next 上下文",
      contextOk,
      contextOk
        ? `选区: ${reader.selected_text.text.slice(0, 24)}…`
        : JSON.stringify(reader ?? null).slice(0, 120),
    );

    // 7. 长消息不撑破聊天窗口
    const longText =
      "今天先读到实验部分，明天接着读附录。这篇的行文比我预期的直接，阅读节奏可以放慢一些，边读边把关键段落放进文献库。".repeat(3) +
      " 附一个超长无空格串压测换行 " + "A".repeat(160);
    await sendChat(page, longText);
    await page.waitForFunction(
      (needle) => document.body.textContent.includes(needle),
      "附一个超长无空格串压测换行",
      { timeout: 15000 },
    );
    await page.waitForTimeout(500);
    const overflow = await page.evaluate(() => {
      const pane = document.querySelector("section .flex-1.overflow-y-auto");
      if (!pane) return { ok: false, detail: "找不到消息容器" };
      const paneOk = pane.scrollWidth <= pane.clientWidth + 2;
      const bubbles = Array.from(pane.querySelectorAll("p"));
      const bad = bubbles.filter((p) => p.scrollWidth > p.clientWidth + 2).length;
      return { ok: paneOk && bad === 0, detail: `pane ${pane.scrollWidth}/${pane.clientWidth}, 溢出气泡 ${bad}` };
    });
    record("长消息/长 token 不横向溢出", overflow.ok, overflow.detail);
    await page.screenshot({ path: shot("chat-long") });

    // 8. 窄屏 390x844：Pet 与聊天窗口在视口内，输入可用
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(400);
    const section = await page.locator('section:has(h2:has-text("阅读 Pet"))').boundingBox();
    const petBox = await page.locator(PET_BUTTON).boundingBox();
    const fits =
      section && petBox &&
      section.x >= 0 && section.y >= 0 &&
      section.x + section.width <= 391 &&
      section.y + section.height <= 845 &&
      petBox.x + petBox.width <= 391;
    record(
      "窄屏下聊天窗口与 Pet 都在视口内",
      Boolean(fits),
      `section=${JSON.stringify(section)}, pet=${JSON.stringify(petBox)}`,
    );
    const inputEditable = await page.locator(CHAT_INPUT).isEditable();
    record("窄屏下输入框可用", inputEditable);
    const mobileOverflow = await page.evaluate(() => {
      const pane = document.querySelector("section .flex-1.overflow-y-auto");
      if (!pane) return false;
      return pane.scrollWidth <= pane.clientWidth + 2;
    });
    record("窄屏下消息不横向溢出", mobileOverflow);
    await page.screenshot({ path: shot("mobile-chat") });
    await page.click('button:has-text("关闭")');
    await page.waitForTimeout(300);
    await page.screenshot({ path: shot("mobile-idle") });

    // 9. 桌面端 Pet：关闭态只沿右缘停靠，打开后仍可自由拖拽
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(400);
    const petBefore = await page.locator(PET_BUTTON).boundingBox();
    await page.mouse.move(petBefore.x + petBefore.width / 2, petBefore.y + petBefore.height / 2);
    await page.mouse.down();
    await page.mouse.move(petBefore.x + petBefore.width / 2, 260, { steps: 12 });
    await page.mouse.up();
    await page.waitForTimeout(250);
    const petAfter = await page.locator(PET_BUTTON).boundingBox();
    const petCenter = {
      x: petAfter.x + petAfter.width / 2,
      y: petAfter.y + petAfter.height / 2,
    };
    record(
      "Pet 关闭态纵向拖动后仍吸附右缘",
      petAfter.x < 1440 && petAfter.x + petAfter.width > 1440 && Math.abs(petCenter.y - 260) < 60,
      `center=(${Math.round(petCenter.x)},${Math.round(petCenter.y)})`,
    );
    record(
      "拖拽松手不误触发开窗",
      (await page.locator('h2:has-text("阅读 Pet")').count()) === 0,
    );
    const savedPos = await page.evaluate(() => window.localStorage.getItem("peinidu.pet.pos"));
    record("拖拽位置写入 localStorage", Boolean(savedPos), savedPos ?? "");

    await page.click(PET_BUTTON);
    await page.waitForSelector('h2:has-text("阅读 Pet")', { timeout: 10000 });
    await page.waitForTimeout(150);
    const petOpened = await page.locator(PET_BUTTON).boundingBox();
    record(
      "Pet 打开后完整进入视口",
      petOpened.x >= 0 && petOpened.x + petOpened.width <= 1441,
      JSON.stringify(petOpened),
    );

    await page.mouse.move(
      petOpened.x + petOpened.width / 2,
      petOpened.y + petOpened.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(420, 260, { steps: 12 });
    await page.mouse.up();
    await page.waitForTimeout(250);
    const petFreelyDragged = await page.locator(PET_BUTTON).boundingBox();
    const freeCenter = {
      x: petFreelyDragged.x + petFreelyDragged.width / 2,
      y: petFreelyDragged.y + petFreelyDragged.height / 2,
    };
    record(
      "Pet 打开态仍可自由拖拽",
      Math.abs(freeCenter.x - 420) < 60 && Math.abs(freeCenter.y - 260) < 60,
      `center=(${Math.round(freeCenter.x)},${Math.round(freeCenter.y)})`,
    );
    record(
      "打开态拖拽不误关闭聊天",
      (await page.locator('h2:has-text("阅读 Pet")').count()) === 1,
    );

    const draggedSection = await page
      .locator('section:has(h2:has-text("阅读 Pet"))')
      .boundingBox();
    const nearOk =
      draggedSection &&
      draggedSection.x >= 0 &&
      draggedSection.y >= 0 &&
      draggedSection.x + draggedSection.width <= 1441 &&
      draggedSection.y + draggedSection.height <= 901;
    record(
      "拖拽后聊天窗口就近弹出且在视口内",
      Boolean(nearOk),
      JSON.stringify(draggedSection),
    );
    await page.screenshot({ path: shot("dragged") });

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(SPRITE, { timeout: 30000 });
    await page.waitForTimeout(400);
    const petReloaded = await page.locator(PET_BUTTON).boundingBox();
    record(
      "刷新后 Pet 恢复右缘停靠并保留纵向位置",
      petReloaded.x < 1440 &&
        petReloaded.x + petReloaded.width > 1440 &&
        Math.abs(petReloaded.y - petFreelyDragged.y) < 12,
      `reloaded=(${Math.round(petReloaded.x)},${Math.round(petReloaded.y)})`,
    );

    // 10. 清空当前论文对话：回到欢迎态
    await page.click(PET_BUTTON);
    await page.waitForSelector('h2:has-text("阅读 Pet")', { timeout: 10000 });
    await page.waitForFunction(
      () => !document.body.textContent.includes("正在进入当前论文上下文"),
      undefined,
      { timeout: 15000 },
    );
    await page.click('button:has-text("清空")');
    await page.waitForFunction(
      () => document.body.textContent.includes("你可以直接说想怎么读这篇论文"),
      undefined,
      { timeout: 10000 },
    );
    record("清空对话后回到欢迎态", true);
  } finally {
    const report = {
      ok: steps.every((s) => s.ok) && pageErrors.length === 0,
      appBase,
      apiBase,
      arxivId,
      steps,
      consoleErrors,
      pageErrors,
    };
    fs.writeFileSync(path.join(outDir, "pet-flow-report.json"), JSON.stringify(report, null, 2));
    console.log(`\n报告: output/playwright/pet-flow-report.json`);
    console.log(`console 错误 ${consoleErrors.length} 条, page 错误 ${pageErrors.length} 条`);
    await browser.close();
    if (!report.ok) process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
