import type { Metadata } from "next";

import { Header } from "@/components/Header";

export const metadata: Metadata = {
  title: "使用教程 | 陪你读",
  description: "从配置服务到检索、阅读、笔记和 Agent 研究的完整使用路径。",
};

const STEPS = [
  {
    number: "01",
    title: "完成一次设置",
    description:
      "模型和翻译服务使用你自己的 Key。论文可以先保存在这台电脑，也可以选择一个本地文件夹长期备份和迁移。",
    note: "Key 只保存在系统凭据库，不会写进论文、笔记或浏览器存储。",
    href: "/config",
    action: "打开设置",
  },
  {
    number: "02",
    title: "检索并确认论文",
    description:
      "输入论文标题、arXiv ID 或链接。出现多个候选时，先核对标题和作者，再确认要读取的论文。",
    note: "确认后会保存原始 PDF，并准备文字和版面信息。",
    href: "/",
    action: "开始检索",
  },
  {
    number: "03",
    title: "在原始 PDF 上阅读",
    description:
      "阅读区始终显示未经改动的原始 PDF。划选英文后会自动翻译，译文和操作留在右侧工作台，不会遮挡论文。",
    note: "触控板可以连续缩放；低置信或扫描页面会明确提示，避免翻译错误文字。",
    href: "/reading",
    action: "进入阅读",
  },
  {
    number: "04",
    title: "把判断写进笔记",
    description:
      "选区可以标记为重要、疑问、方法或结论，并附上 Markdown 笔记。整篇论文也有一份长期主笔记。",
    note: "笔记跟随论文保存，加入或移出专题都不会删除它。",
    href: "/library",
    action: "查看文献库",
  },
  {
    number: "05",
    title: "让 Pet 和 Agent 接着研究",
    description:
      "阅读时用 Pet 快速追问；需要方法分析、外部检索或跨笔记研究时，进入 Agent 工作台继续同一段对话。",
    note: "回答会区分论文原文、外部网页和你的笔记，并保留可核对的证据。",
    href: "/agent",
    action: "进入 Agent",
  },
] as const;

const DATA_BOUNDARIES = [
  {
    term: "论文与笔记",
    description: "默认留在这台电脑；选择本地文件夹后，可以自行备份和迁移。",
  },
  {
    term: "API Key",
    description: "由你提供，并保存在系统凭据库；不会进入论文目录、对话或网页存储。",
  },
  {
    term: "重型能力",
    description: "浏览器自动化和中文 PDF 导出按需启用，不影响日常检索、阅读、翻译和 Agent。",
  },
] as const;

export default function GuidePage() {
  return (
    <>
      <Header />
      <main className="mx-auto max-w-5xl px-4 pb-20 pt-24 sm:px-6 md:pt-28 lg:px-8">
        <header className="max-w-3xl border-b border-[hsl(var(--border))] pb-9">
          <p className="font-mono text-xs tracking-[0.12em] text-[hsl(var(--muted-foreground))]">
            使用教程
          </p>
          <h1 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">
            如何使用陪你读
          </h1>
          <p className="mt-4 max-w-2xl text-sm leading-7 text-[hsl(var(--muted-foreground))]">
            最短路径是先完成设置，再检索一篇论文。之后在原始 PDF
            上阅读、划选翻译和记笔记，需要深入研究时再交给 Pet 或 Agent。
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <a
              href="/config"
              className="inline-flex min-h-10 items-center rounded-md bg-[hsl(var(--primary))] px-4 text-sm font-medium text-[hsl(var(--primary-foreground))] transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--focus-ring))] focus-visible:ring-offset-2"
            >
              先去设置
            </a>
            <a
              href="/"
              className="inline-flex min-h-10 items-center rounded-md border border-[hsl(var(--border))] px-4 text-sm text-[hsl(var(--foreground))] transition-colors hover:bg-[hsl(var(--muted))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--focus-ring))] focus-visible:ring-offset-2"
            >
              直接检索
            </a>
          </div>
        </header>

        <section aria-labelledby="guide-steps-title" className="mt-12">
          <div className="flex items-baseline justify-between gap-4">
            <h2 id="guide-steps-title" className="text-lg font-semibold tracking-tight">
              完整使用路径
            </h2>
            <span className="font-mono text-xs text-[hsl(var(--muted-foreground))]">
              5 个步骤
            </span>
          </div>

          <ol className="mt-4 border-t border-[hsl(var(--border))]">
            {STEPS.map((step) => (
              <li
                key={step.number}
                className="grid gap-4 border-b border-[hsl(var(--border))] py-7 sm:grid-cols-[3.25rem_minmax(0,1fr)] sm:gap-6 sm:py-8"
              >
                <span
                  aria-hidden="true"
                  className="font-mono text-sm text-[hsl(var(--reader-accent))]"
                >
                  {step.number}
                </span>
                <div className="min-w-0">
                  <h3 className="text-base font-semibold tracking-tight">{step.title}</h3>
                  <p className="mt-2 max-w-3xl text-sm leading-7 text-[hsl(var(--foreground))]/85">
                    {step.description}
                  </p>
                  <p className="mt-2 max-w-3xl text-xs leading-6 text-[hsl(var(--muted-foreground))]">
                    {step.note}
                  </p>
                  <a
                    href={step.href}
                    className="mt-4 inline-flex min-h-9 items-center text-sm font-medium text-[hsl(var(--reader-accent))] underline decoration-[hsl(var(--reader-accent))]/30 underline-offset-4 hover:decoration-[hsl(var(--reader-accent))] focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--focus-ring))] focus-visible:ring-offset-2"
                  >
                    {step.action}
                    <span aria-hidden="true" className="ml-1.5">
                      →
                    </span>
                  </a>
                </div>
              </li>
            ))}
          </ol>
        </section>

        <section aria-labelledby="guide-data-title" className="mt-14">
          <h2 id="guide-data-title" className="text-lg font-semibold tracking-tight">
            你的数据在哪里
          </h2>
          <dl className="mt-4 divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
            {DATA_BOUNDARIES.map((item) => (
              <div
                key={item.term}
                className="grid gap-2 py-5 sm:grid-cols-[8rem_minmax(0,1fr)] sm:gap-6"
              >
                <dt className="text-sm font-medium">{item.term}</dt>
                <dd className="max-w-3xl text-sm leading-6 text-[hsl(var(--muted-foreground))]">
                  {item.description}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      </main>
    </>
  );
}
