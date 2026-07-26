"use client";

import Image from "next/image";
import { useTheme } from "next-themes";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CURRENT_READING_EVENT,
  readCurrentReading,
  type CurrentReading,
} from "@/lib/currentReading";

export function Header() {
  const pathname = usePathname();
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [currentReading, setCurrentReading] = useState<CurrentReading | null>(null);

  // 避免 hydration mismatch（主题切换按钮需客户端挂载后显示）
  useEffect(() => setMounted(true), []);

  useEffect(() => {
    const refresh = () => setCurrentReading(readCurrentReading());
    refresh();
    window.addEventListener(CURRENT_READING_EVENT, refresh);
    window.addEventListener("storage", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      window.removeEventListener(CURRENT_READING_EVENT, refresh);
      window.removeEventListener("storage", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  const navigation = [
    { href: "/", label: "检索", active: pathname === "/" },
    {
      href: "/reading",
      label: "阅读",
      active: pathname === "/reading" || pathname.startsWith("/paper/"),
    },
    {
      href: "/library",
      label: "文献库",
      active: pathname === "/library" || pathname.startsWith("/library/"),
    },
    {
      href: "/agent",
      label: "Agent",
      active: pathname === "/agent" || pathname.startsWith("/agent/"),
    },
    { href: "/config", label: "设置", active: pathname === "/config" },
    { href: "/guide", label: "教程", active: pathname === "/guide" },
  ];

  const dark = mounted && resolvedTheme === "dark";
  const onHome = pathname === "/";

  return (
    <header className="app-header">
      <nav
        className={`app-header-inner ${onHome ? "app-header-inner-home" : ""}`}
        aria-label="主导航"
      >
        {!onHome && (
          <a
            href="/"
            className="app-wordmark"
            aria-label="陪你读"
          >
            <span aria-hidden="true" className="home-wordmark app-wordmark-text">
              <span>陪你</span>
              <span className="home-wordmark-accent">读</span>
            </span>
            <Image
              src="/mascot/home-mascot.png"
              alt=""
              width={890}
              height={1095}
              aria-hidden="true"
              className="app-wordmark-mascot"
            />
          </a>
        )}
        <div className="app-header-actions">
          <div className="app-navigation">
            {navigation.map((item) => (
              <a
                key={item.href}
                href={item.href}
                title={item.href === "/reading" && currentReading
                  ? `继续阅读：${currentReading.title}`
                  : undefined}
                aria-current={item.active ? "page" : undefined}
                className={`app-nav-link ${item.active ? "app-nav-link-active" : ""}`}
              >
                {item.label}
              </a>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setTheme(dark ? "light" : "dark")}
            className="app-theme-toggle"
            aria-label={dark ? "切换到浅色模式" : "切换到深色模式"}
            title={dark ? "浅色模式" : "深色模式"}
          >
            {dark ? (
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="3.25" />
                <path d="M12 2.5v2M12 19.5v2M4.4 4.4l1.4 1.4M18.2 18.2l1.4 1.4M2.5 12h2M19.5 12h2M4.4 19.6l1.4-1.4M18.2 5.8l1.4-1.4" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z" />
              </svg>
            )}
          </button>
        </div>
      </nav>
    </header>
  );
}
