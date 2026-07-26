"use client";

import { useState } from "react";

import { Header } from "@/components/Header";
import { FadeUp } from "@/components/FadeUp";
import { LocalLibrarySettings } from "@/components/LocalLibrarySettings";
import {
  ProviderConfig,
  type ProviderConfigSection,
} from "@/components/ProviderConfig";
import {
  SettingsSectionIcon,
  type SettingsSectionIconName,
} from "@/components/SettingsSectionIcon";

type SettingsSection = "library" | ProviderConfigSection;

const SETTINGS_SECTIONS: {
  id: SettingsSection;
  label: string;
  description: string;
  icon: SettingsSectionIconName;
}[] = [
  {
    id: "library",
    label: "文献与存储",
    description: "选择论文和笔记的保存位置。数据默认留在这台电脑，也可以写入你指定的文件夹。",
    icon: "library",
  },
  {
    id: "models",
    label: "模型与翻译",
    description: "配置自己的模型服务、DeepLX 和各类研究任务所使用的模型。",
    icon: "models",
  },
  {
    id: "tools",
    label: "工具与解析",
    description: "管理 MCP 工具与 MinerU 文档解析能力。",
    icon: "tools",
  },
  {
    id: "advanced",
    label: "高级",
    description: "管理访问令牌、兼容提示词和运行参数。",
    icon: "advanced",
  },
];

export default function ConfigPage() {
  const [activeSection, setActiveSection] = useState<SettingsSection>("library");
  const active = SETTINGS_SECTIONS.find((section) => section.id === activeSection)!;

  return (
    <>
      <Header />
      <main className="min-h-screen bg-[hsl(var(--background))]">
        <div className="mx-auto max-w-6xl px-4 pb-20 pt-24 sm:px-6 md:pt-28 lg:px-8">
          <FadeUp>
            <h1 className="text-2xl font-semibold tracking-tight">设置</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[hsl(var(--muted-foreground))]">
              管理文献保存位置、模型和研究工具。API Key 只保存在这台电脑。
            </p>
          </FadeUp>

          <FadeUp delay={1} className="mt-8">
            <div className="border-t border-[hsl(var(--border))] min-[560px]:grid min-[560px]:grid-cols-[10.5rem_minmax(0,1fr)] min-[560px]:gap-6 md:grid-cols-[13rem_minmax(0,1fr)] md:gap-10 lg:gap-12">
              <aside className="hidden border-r border-[hsl(var(--border))] pr-3 min-[560px]:block md:pr-5">
                <nav
                  aria-label="设置分类"
                  className="sticky top-20 space-y-1 py-7"
                >
                  {SETTINGS_SECTIONS.map((section) => (
                    <button
                      key={section.id}
                      type="button"
                      aria-current={activeSection === section.id ? "page" : undefined}
                      aria-pressed={activeSection === section.id}
                      onClick={() => setActiveSection(section.id)}
                      className={`flex min-h-11 w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition-[background-color,color] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] focus-visible:ring-offset-2 ${
                        activeSection === section.id
                          ? "bg-[hsl(var(--reader-accent-soft))] font-medium text-[hsl(var(--reader-accent))]"
                          : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]/55 hover:text-[hsl(var(--foreground))]"
                      }`}
                    >
                      <SettingsSectionIcon name={section.icon} />
                      <span>{section.label}</span>
                    </button>
                  ))}
                </nav>
              </aside>

              <section
                aria-labelledby={`settings-${activeSection}-title`}
                className="min-w-0 py-6 min-[560px]:py-8"
              >
                <div className="mb-7 min-[560px]:hidden">
                  <label
                    htmlFor="settings-section"
                    className="mb-2 block text-xs font-medium text-[hsl(var(--muted-foreground))]"
                  >
                    设置分类
                  </label>
                  <select
                    id="settings-section"
                    aria-label="设置分类"
                    value={activeSection}
                    onChange={(event) =>
                      setActiveSection(event.target.value as SettingsSection)
                    }
                    className="min-h-11 w-full rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 text-sm text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))]"
                  >
                    {SETTINGS_SECTIONS.map((section) => (
                      <option key={section.id} value={section.id}>
                        {section.label}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="mb-8 flex gap-3 border-b border-[hsl(var(--border))] pb-6">
                  <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-[hsl(var(--muted))]/55 text-[hsl(var(--muted-foreground))]">
                    <SettingsSectionIcon name={active.icon} />
                  </span>
                  <div>
                    <h2
                      id={`settings-${activeSection}-title`}
                      className="text-xl font-semibold tracking-tight"
                    >
                      {active.label}
                    </h2>
                    <p className="mt-1.5 max-w-2xl text-sm leading-6 text-[hsl(var(--muted-foreground))]">
                      {active.description}
                    </p>
                  </div>
                </div>

                <div className={activeSection === "library" ? "" : "hidden"}>
                  <LocalLibrarySettings />
                </div>
                <div className={activeSection === "library" ? "hidden" : ""}>
                  <ProviderConfig
                    section={
                      activeSection === "library" ? "models" : activeSection
                    }
                  />
                </div>
              </section>
            </div>
          </FadeUp>
        </div>
      </main>
    </>
  );
}
