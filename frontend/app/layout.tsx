import type { Metadata } from "next";
import { ThemeProvider } from "next-themes";
import "./globals.css";

export const metadata: Metadata = {
  title: "陪你读 — 科研论文辅助阅读",
  description: "原始 PDF 划选翻译、Markdown 笔记与 Pet 研究助手，数据默认留在你的电脑",
  icons: {
    icon: "/icon.svg",
    shortcut: "/icon.svg",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <style>{`
          :root { --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
        `}</style>
      </head>
      <body className="min-h-screen antialiased">
        <ThemeProvider attribute="class" defaultTheme="light" enableSystem disableTransitionOnChange>
          {/* Grid pattern background — 移植自博客 Layout.astro */}
          <svg
            aria-hidden="true"
            className="grid-pattern-bg fill-gray-400/30 stroke-gray-400/30 [mask-image:radial-gradient(circle_at_center,rgba(0,0,0,0.6),transparent)]"
          >
            <defs>
              <pattern id="grid" width="25" height="25" patternUnits="userSpaceOnUse" x="-1" y="-1">
                <path d="M.5 25V.5H25" fill="none" strokeDasharray="4 2" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" strokeWidth="0" fill="url(#grid)" />
          </svg>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
