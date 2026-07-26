"use client";

import { useState, useEffect } from "react";
import {
  createMinerUPaper,
  deleteConfigCredential,
  getConfig,
  saveConfig,
  discoverModels,
  testDeepLXConfig,
  testMcpServer,
  type AppConfigData,
  type DiscoveredModel,
  type MCPServerConfig,
  type ProviderConfig,
} from "@/lib/api";

const TASK_LABELS: {
  key: keyof AppConfigData["task_models"];
  label: string;
  placeholder?: string;
}[] = [
  { key: "translation", label: "翻译" },
  { key: "agent_summary", label: "摘要 Agent" },
  { key: "agent_reproducibility", label: "可复现性 Agent" },
  { key: "agent_improvement", label: "改进点 Agent" },
  { key: "agent_highlights", label: "亮点 Agent" },
  { key: "agent_chat", label: "Pet 对话", placeholder: "留空 = 跟随默认模型" },
  { key: "agent_intent", label: "意图分类", placeholder: "留空 = 跟随默认模型" },
];

const PROVIDER_TYPES = ["openai", "anthropic", "deepseek", "gemini"] as const;
const MCP_TRANSPORTS = ["stdio", "http"] as const;
const MINERU_MODES = ["agent_lite", "standard"] as const;
const MINERU_LANGUAGES = ["en", "ch", "latin", "japan", "korean"] as const;
const MCP_PERMISSION_OPTIONS: {
  value: MCPServerConfig["permission_scopes"][number];
  label: string;
}[] = [
  { value: "mcp_tool", label: "MCP/工具调用" },
  { value: "external_search", label: "外部检索" },
  { value: "long_task", label: "长任务" },
  { value: "browser_control", label: "浏览器控制" },
];
const ADMIN_TOKEN_STORAGE_KEY = "peinidu_admin_token";
function isSecretPlaceholder(value: string): boolean {
  return value.includes("***") || value.includes("•");
}

const DEFAULT_TRANSLATION_PROMPT = `你是一位严谨的学术论文翻译。我会给你三段原文：上一段、当前段、下一段。
请只翻译【当前段】，不要翻译上一段和下一段。
上一段和下一段仅作为上下文，帮助你理解指代、术语和行文逻辑。

要求：
1. 保持学术术语准确，专业名词首次出现时附原文（如：注意力机制 (attention mechanism)）。
2. 保留原文中的公式、图表引用编号、引用标记 [n] 不翻译。
3. 不要添加解释性内容，不要意译扩写，忠实于当前段原文。
4. 输出仅为当前段的中文译文，不要输出其他任何内容。`;

export type ProviderConfigSection = "models" | "tools" | "advanced";

export function ProviderConfig({
  section = "models",
}: {
  section?: ProviderConfigSection;
}) {
  const [config, setConfig] = useState<AppConfigData | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [adminToken, setAdminToken] = useState("");
  const [mcpTests, setMcpTests] = useState<
    Record<number, { loading: boolean; ok?: boolean; message: string }>
  >({});

  // 当前选中的 provider 索引（tab 切换）
  const [selectedIdx, setSelectedIdx] = useState(0);

  // 模型发现状态（按当前选中 provider）
  const [discovering, setDiscovering] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<DiscoveredModel[]>([]);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  // api_key 编辑态：用户在 input 里输的内容（独立于 config，保存时合并）
  // 脱敏值（含 ***）不能用于发现/保存，需用户重新输入真实 key
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [mineruTokenInput, setMineruTokenInput] = useState("");
  const [deeplxKeyInput, setDeepLXKeyInput] = useState("");
  const [testingDeepLX, setTestingDeepLX] = useState(false);
  const [mineruParsing, setMineruParsing] = useState(false);
  const [mineruParseMessage, setMineruParseMessage] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [mineruParseForm, setMineruParseForm] = useState({
    url: "https://cdn-mineru.openxlab.org.cn/demo/example.pdf",
    title: "",
    file_name: "example.pdf",
    page_range: "1-2",
    language: "en",
  });

  useEffect(() => {
    (async () => {
      const storedToken = window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
      setAdminToken(storedToken);
      try {
        const cfg = await getConfig(storedToken);
        setConfig(cfg);
        const p = cfg.llm_providers[0];
        if (p) setApiKeyInput(p.api_key || "");
        setMineruTokenInput(cfg.mineru.api_token || "");
        setDeepLXKeyInput(cfg.deeplx.api_key || "");
      } catch (e) {
        setMessage({ type: "err", text: (e as Error).message });
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const reloadConfig = async (token: string) => {
    setLoading(true);
    setMessage(null);
    try {
      const cfg = await getConfig(token);
      window.localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
      setConfig(cfg);
      setSelectedIdx(0);
      const p = cfg.llm_providers[0];
      setApiKeyInput(p?.api_key || "");
      setMineruTokenInput(cfg.mineru.api_token || "");
      setDeepLXKeyInput(cfg.deeplx.api_key || "");
      setDiscoveredModels([]);
      setDiscoverError(null);
      setMessage({ type: "ok", text: "管理员 token 已应用" });
    } catch (e) {
      setMessage({ type: "err", text: (e as Error).message });
    } finally {
      setLoading(false);
    }
  };

  // 切换 provider tab：清发现结果 + 同步 api_key 输入框
  const switchProvider = (idx: number) => {
    setSelectedIdx(idx);
    setDiscoveredModels([]);
    setDiscoverError(null);
    if (config) setApiKeyInput(config.llm_providers[idx]?.api_key || "");
  };

  // 根据 provider type 推断默认 base_url
  function defaultBaseUrl(type: string): string {
    const map: Record<string, string> = {
      deepseek: "https://api.deepseek.com",
      openai: "https://api.openai.com/v1",
      anthropic: "https://api.anthropic.com",
      gemini: "https://generativelanguage.googleapis.com/v1",
    };
    return map[type] || "";
  }

  // 当前选中的 provider（可能为空）
  const current = config?.llm_providers[selectedIdx];

  // 修改当前 provider 的某个字段
  const updateCurrentProvider = (patch: Partial<ProviderConfig>) => {
    if (!config || !current) return;
    const providers = [...config.llm_providers];
    providers[selectedIdx] = { ...current, ...patch };
    setConfig({ ...config, llm_providers: providers });
  };

  // 新增 provider
  const addProvider = () => {
    if (!config) return;
    const newProvider: ProviderConfig = {
      name: `provider-${config.llm_providers.length + 1}`,
      type: "openai",
      api_key: "",
      api_base: null,
      models: [],
    };
    const newIdx = config.llm_providers.length;
    setConfig({ ...config, llm_providers: [...config.llm_providers, newProvider] });
    setSelectedIdx(newIdx);
    setDiscoveredModels([]);
    setDiscoverError(null);
    setApiKeyInput("");
  };

  // 删除 provider（≥2 个才允许，防止删空）
  const removeProvider = () => {
    if (!config || config.llm_providers.length <= 1) return;
    if (!confirm(`确认删除 provider "${current?.name}"？`)) return;
    const providers = config.llm_providers.filter((_, i) => i !== selectedIdx);
    const newIdx = Math.max(0, selectedIdx - 1);
    const wasDefault = config.default_provider === current?.name;
    setConfig({
      ...config,
      llm_providers: providers,
      // 删的是 default_provider → 回退到第一个
      default_provider: wasDefault ? providers[0].name : config.default_provider,
    });
    setSelectedIdx(newIdx);
    setDiscoveredModels([]);
    setDiscoverError(null);
    setApiKeyInput(providers[newIdx]?.api_key || "");
    if (wasDefault) {
      setMessage({ type: "err", text: `已删除 default provider，自动回退到 "${providers[0].name}"，请确认后保存` });
    }
  };

  // 发现模型（用当前 provider 的 base_url + 用户输入的 key）
  const handleDiscover = async () => {
    if (!current) return;
    const baseUrl = current.api_base || defaultBaseUrl(current.type);
    if (!baseUrl.trim()) {
      setDiscoverError("请先填写 base_url");
      return;
    }
    // 脱敏值不能用于发现
    const keyToUse =
      isSecretPlaceholder(apiKeyInput) || !apiKeyInput.trim() ? "" : apiKeyInput;
    if (!keyToUse && !current.api_key_configured) {
      setDiscoverError("请先填写并保存 API Key");
      return;
    }
    setDiscovering(true);
    setDiscoverError(null);
    try {
      const models = await discoverModels(
        baseUrl,
        keyToUse,
        current.name,
        adminToken,
      );
      setDiscoveredModels(models);
      // 同步把发现的模型 merge 进该 provider 的 models 列表
      const existing = new Set(current.models);
      const merged = [...current.models];
      for (const m of models) {
        if (!existing.has(m.id)) {
          merged.push(m.id);
          existing.add(m.id);
        }
      }
      updateCurrentProvider({ models: merged, api_base: baseUrl });
      if (models.length === 0) setDiscoverError("未发现任何模型");
    } catch (e) {
      setDiscoverError((e as Error).message);
      setDiscoveredModels([]);
    } finally {
      setDiscovering(false);
    }
  };

  // 保存配置
  const handleSave = async () => {
    if (!config) return;
    setSaving(true);
    setMessage(null);
    try {
      const cfg = { ...config };
      // 把 apiKeyInput 合并进当前选中 provider（其他 provider 的 key 保持脱敏值，后端会保留原 key）
      const providers = [...cfg.llm_providers];
      if (providers[selectedIdx]) {
        // 只有用户输入了新 key（不含 ***）才覆盖
        if (apiKeyInput && !isSecretPlaceholder(apiKeyInput)) {
          providers[selectedIdx] = { ...providers[selectedIdx], api_key: apiKeyInput };
        }
        // 确保 api_base 非空时写入
        if (!providers[selectedIdx].api_base && current) {
          providers[selectedIdx] = {
            ...providers[selectedIdx],
            api_base: defaultBaseUrl(current.type) || null,
          };
        }
      }
      cfg.llm_providers = providers;
      if (mineruTokenInput && !isSecretPlaceholder(mineruTokenInput)) {
        cfg.mineru = { ...cfg.mineru, api_token: mineruTokenInput };
      }
      if (deeplxKeyInput && !isSecretPlaceholder(deeplxKeyInput)) {
        cfg.deeplx = { ...cfg.deeplx, api_key: deeplxKeyInput };
      }
      const result = await saveConfig(cfg, adminToken);
      const fresh = await getConfig(adminToken);
      setConfig(fresh);
      setApiKeyInput(fresh.llm_providers[selectedIdx]?.api_key || "");
      setMineruTokenInput(fresh.mineru.api_token || "");
      setDeepLXKeyInput(fresh.deeplx.api_key || "");
      setMessage({ type: "ok", text: result.message });
    } catch (e) {
      setMessage({ type: "err", text: (e as Error).message });
    } finally {
      setSaving(false);
    }
  };

  // 更新某个 task 的模型
  const updateTaskModel = (key: keyof AppConfigData["task_models"], model: string) => {
    if (!config) return;
    setConfig({ ...config, task_models: { ...config.task_models, [key]: model } });
  };

  const updateMineru = (patch: Partial<AppConfigData["mineru"]>) => {
    if (!config) return;
    setConfig({ ...config, mineru: { ...config.mineru, ...patch } });
  };

  const handleMineruParse = async () => {
    if (!config) return;
    setMineruParsing(true);
    setMineruParseMessage(null);
    try {
      const cfg = { ...config };
      if (mineruTokenInput && !isSecretPlaceholder(mineruTokenInput)) {
        cfg.mineru = { ...cfg.mineru, api_token: mineruTokenInput };
      }
      await saveConfig(cfg, adminToken);
      setConfig(cfg);
      const paper = await createMinerUPaper({
        url: mineruParseForm.url.trim(),
        title: mineruParseForm.title.trim() || undefined,
        file_name: mineruParseForm.file_name.trim() || undefined,
        page_range: mineruParseForm.page_range.trim() || undefined,
        language: mineruParseForm.language.trim() || undefined,
      });
      setMineruParseMessage({ type: "ok", text: "解析完成，正在打开阅读页" });
      window.location.href = `/paper/${encodeURIComponent(paper.arxiv_id)}`;
    } catch (e) {
      setMineruParseMessage({ type: "err", text: (e as Error).message });
    } finally {
      setMineruParsing(false);
    }
  };

  const handleDeleteCredential = async (
    kind: "llm_provider" | "mineru" | "deeplx",
    providerName?: string,
  ) => {
    if (!confirm("确认从这台电脑的系统凭据库删除该 Key？")) return;
    setMessage(null);
    try {
      await deleteConfigCredential(kind, providerName, adminToken);
      const fresh = await getConfig(adminToken);
      setConfig(fresh);
      setApiKeyInput(fresh.llm_providers[selectedIdx]?.api_key || "");
      setMineruTokenInput(fresh.mineru.api_token || "");
      setDeepLXKeyInput(fresh.deeplx.api_key || "");
      setMessage({ type: "ok", text: "凭据已从系统凭据库删除" });
    } catch (e) {
      setMessage({ type: "err", text: (e as Error).message });
    }
  };

  const handleDeepLXTest = async () => {
    if (deeplxKeyInput && !isSecretPlaceholder(deeplxKeyInput)) {
      setMessage({ type: "err", text: "请先保存新的 DeepLX Key，再测试连接" });
      return;
    }
    setTestingDeepLX(true);
    setMessage(null);
    try {
      await testDeepLXConfig(adminToken);
      setMessage({ type: "ok", text: "DeepLX 翻译连接正常" });
    } catch (e) {
      setMessage({ type: "err", text: (e as Error).message });
    } finally {
      setTestingDeepLX(false);
    }
  };

  const updateMcpServer = (idx: number, patch: Partial<MCPServerConfig>) => {
    if (!config) return;
    const servers = [...config.mcp_servers];
    servers[idx] = { ...servers[idx], ...patch };
    setConfig({ ...config, mcp_servers: servers });
  };

  const addMcpServer = () => {
    if (!config) return;
    const next: MCPServerConfig = {
      name: `mcp-${config.mcp_servers.length + 1}`,
      transport: "stdio",
      command: "",
      args: [],
      url: null,
      enabled: false,
      tool_name: "",
      timeout_seconds: 12,
      permission_scopes: ["mcp_tool"],
      allowed_tools: [],
    };
    setMcpTests({});
    setConfig({ ...config, mcp_servers: [...config.mcp_servers, next] });
  };

  const removeMcpServer = (idx: number) => {
    if (!config) return;
    const server = config.mcp_servers[idx];
    if (!confirm(`确认删除 MCP server "${server.name || "(未命名)"}"？`)) return;
    setMcpTests({});
    setConfig({
      ...config,
      mcp_servers: config.mcp_servers.filter((_, i) => i !== idx),
    });
  };

  const testMcpConnection = async (idx: number) => {
    if (!config) return;
    const server = config.mcp_servers[idx];
    setMcpTests((prev) => ({ ...prev, [idx]: { loading: true, message: "正在连接…" } }));
    try {
      const result = await testMcpServer(server, adminToken || undefined);
      const toolNames = result.tools.slice(0, 5).map((t) => t.name).join("、");
      const message = result.ok
        ? `连接成功（${result.elapsed_ms}ms）：${result.tools.length} 个工具` +
          (toolNames ? ` — ${toolNames}` : "") +
          (result.chosen_tool ? `；将调用 ${result.chosen_tool}` : "") +
          (result.note ? `。${result.note}` : "")
        : result.error || "连接失败";
      setMcpTests((prev) => ({ ...prev, [idx]: { loading: false, ok: result.ok, message } }));
    } catch (e) {
      setMcpTests((prev) => ({
        ...prev,
        [idx]: { loading: false, ok: false, message: (e as Error).message || "测试请求失败" },
      }));
    }
  };

  const toggleMcpPermission = (
    idx: number,
    scope: MCPServerConfig["permission_scopes"][number],
    checked: boolean,
  ) => {
    if (!config) return;
    const server = config.mcp_servers[idx];
    const currentScopes = server.permission_scopes;
    const permission_scopes = checked
      ? Array.from(new Set([...currentScopes, scope]))
      : currentScopes.length > 1
        ? currentScopes.filter((item) => item !== scope)
        : currentScopes;
    updateMcpServer(idx, { permission_scopes });
  };

  if (loading) {
    return (
      <p className="text-sm font-mono text-[hsl(var(--muted-foreground))] animate-pulse">
        加载配置中…
      </p>
    );
  }
  if (!config) {
    return (
      <div className="space-y-4">
        <div>
          <h2 className="text-lg font-medium tracking-tight">管理员访问</h2>
          <p className="mt-1 text-sm leading-6 text-[hsl(var(--muted-foreground))]">
            输入本地 Core 的管理令牌后重新读取设置。
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            type="password"
            value={adminToken}
            onChange={(e) => setAdminToken(e.target.value)}
            placeholder="PEINIDU_ADMIN_TOKEN"
            className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
          />
          <button
            onClick={() => reloadConfig(adminToken)}
            className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono hover:bg-[hsl(var(--muted))] transition-colors"
          >
            应用 token
          </button>
        </div>
        {message && (
          <p className="text-xs font-mono text-red-500">{message.text}</p>
        )}
      </div>
    );
  }

  // 任务模型 dropdown 选项 = 所有 provider 的 models 合并去重
  const allModelOptions = Array.from(
    new Set(config.llm_providers.flatMap((p) => p.models)),
  ).sort();

  const baseUrlValue = current?.api_base || (current ? defaultBaseUrl(current.type) : "");

  return (
    <div className="space-y-10">
      {/* 管理员访问 */}
      <section
        hidden={section !== "advanced"}
        className="border-b border-[hsl(var(--border))] pb-10"
      >
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">管理员访问</h2>
          <p className="mt-1 text-sm leading-6 text-[hsl(var(--muted-foreground))]">
            只在需要修改受保护配置时使用，令牌保存在当前浏览器。
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            type="password"
            value={adminToken}
            onChange={(e) => setAdminToken(e.target.value)}
            placeholder="PEINIDU_ADMIN_TOKEN"
            className="min-w-0 flex-1 rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
          />
          <button
            onClick={() => reloadConfig(adminToken)}
            className="rounded-md border border-[hsl(var(--border))] px-3 py-2 text-xs font-mono hover:bg-[hsl(var(--muted))] transition-colors"
          >
            应用 token
          </button>
        </div>
      </section>

      {/* Provider 设置 */}
      <section
        hidden={section !== "models"}
        className="border-b border-[hsl(var(--border))] pb-10"
      >
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">Provider 设置</h2>
        </div>
        {config.credential_storage?.mode === "system" && (
          <p
            className={`mb-4 text-xs font-mono ${
              config.credential_storage.available
                ? "text-[hsl(var(--muted-foreground))]"
                : "text-red-500"
            }`}
          >
            {config.credential_storage.available
              ? "API Key 只保存在这台电脑的系统凭据库，不写入配置文件或文献库。"
              : "系统凭据库当前不可用；为避免明文落盘，新的 API Key 不会保存。"}
          </p>
        )}

        {/* Provider tab 列表 */}
        <div className="flex flex-wrap items-center gap-2 mb-4 pb-3 border-b border-[hsl(var(--border))]">
          {config.llm_providers.map((p, i) => (
            <button
              key={i}
              onClick={() => switchProvider(i)}
              className={`rounded-md px-3 py-1.5 text-xs font-mono transition-colors border ${
                i === selectedIdx
                  ? "bg-[hsl(var(--foreground))] text-[hsl(var(--background))] border-[hsl(var(--foreground))]"
                  : "border-[hsl(var(--border))] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]"
              }`}
            >
              {p.name || "(未命名)"}
            </button>
          ))}
          <button
            onClick={addProvider}
            className="rounded-md border border-dashed border-[hsl(var(--border))] px-3 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
          >
            + 新增 provider
          </button>
        </div>

        {/* 当前 provider 编辑区 */}
        {current && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  名称
                </label>
                <input
                  type="text"
                  value={current.name}
                  onChange={(e) => updateCurrentProvider({ name: e.target.value })}
                  placeholder="deepseek-official"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  类型
                </label>
                <select
                  value={current.type}
                  onChange={(e) => updateCurrentProvider({ type: e.target.value })}
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                >
                  {PROVIDER_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div>
              <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                Base URL
              </label>
              <input
                type="text"
                value={baseUrlValue}
                onChange={(e) => updateCurrentProvider({ api_base: e.target.value || null })}
                placeholder="https://api.deepseek.com"
                className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
              />
            </div>
            <div>
              <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                API Key{" "}
                {current.api_key_configured && (
                  <span className="text-green-600">（已配置）</span>
                )}
              </label>
              <input
                type="password"
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                onFocus={() => {
                  if (isSecretPlaceholder(apiKeyInput)) setApiKeyInput("");
                }}
                placeholder="sk-..."
                className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
              />
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={handleDiscover}
                disabled={discovering}
                className="rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-xs font-mono hover:bg-[hsl(var(--muted))] transition-colors disabled:opacity-40"
              >
                {discovering ? "发现中…" : "发现模型"}
              </button>
              {current.api_key_configured &&
                config.credential_storage?.mode === "system" && (
                  <button
                    onClick={() =>
                      handleDeleteCredential("llm_provider", current.name)
                    }
                    className="rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-xs font-mono text-red-500 hover:bg-[hsl(var(--muted))] transition-colors"
                  >
                    删除 Key
                  </button>
                )}
              {discoveredModels.length > 0 && (
                <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                  找到 {discoveredModels.length} 个可用模型
                </span>
              )}
              {discoverError && (
                <span className="text-xs font-mono text-red-500">{discoverError}</span>
              )}
            </div>
            {/* 发现的模型列表 */}
            {discoveredModels.length > 0 && (
              <div className="rounded-md border border-[hsl(var(--border))] p-3 max-h-48 overflow-y-auto">
                <p className="text-xs font-mono text-[hsl(var(--muted-foreground))] mb-2">
                  可用模型：
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {discoveredModels.map((m) => (
                    <span
                      key={m.id}
                      className="rounded border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/50 px-2 py-0.5 text-xs font-mono"
                    >
                      {m.id}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {/* 已配置的模型列表（来自 config + 发现 merge） */}
            {current.models.length > 0 && (
              <div>
                <p className="text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  该 provider 的模型：
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {current.models.map((m) => (
                    <span
                      key={m}
                      className="rounded border border-[hsl(var(--border))] px-2 py-0.5 text-xs font-mono"
                    >
                      {m}
                      <button
                        onClick={() =>
                          updateCurrentProvider({
                            models: current.models.filter((x) => x !== m),
                          })
                        }
                        className="ml-1.5 text-[hsl(var(--muted-foreground))] hover:text-red-500"
                        aria-label={`移除 ${m}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            )}
            {/* 删除按钮（≥2 个 provider 才显示） */}
            {config.llm_providers.length > 1 && (
              <button
                onClick={removeProvider}
                className="text-xs font-mono text-red-500 hover:underline"
              >
                删除该 provider
              </button>
            )}
          </div>
        )}
      </section>

      {/* DeepLX 基础翻译 */}
      <section
        hidden={section !== "models"}
        className="border-b border-[hsl(var(--border))] pb-10"
      >
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">DeepLX 基础翻译</h2>
        </div>
        <p className="mb-4 text-xs font-mono leading-relaxed text-[hsl(var(--muted-foreground))]">
          只负责 PDF 划选与长段落的基础翻译；Pet、Agent 与论文分析仍使用上面的
          LiteLLM Provider。
        </p>
        <div className="space-y-4 rounded-md border border-[hsl(var(--border))] p-4">
          <div>
            <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
              请求地址
            </label>
            <input
              type="url"
              value={config.deeplx.base_url}
              onChange={(e) =>
                setConfig({
                  ...config,
                  deeplx: { ...config.deeplx, base_url: e.target.value },
                })
              }
              placeholder="https://api.deeplx.org"
              className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
          </div>
          <div>
            <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
              API Key{" "}
              {config.deeplx.api_key_configured && (
                <span className="text-green-600">（已配置）</span>
              )}
            </label>
            <input
              type="password"
              value={deeplxKeyInput}
              onChange={(e) => setDeepLXKeyInput(e.target.value)}
              onFocus={() => {
                if (isSecretPlaceholder(deeplxKeyInput)) {
                  setDeepLXKeyInput("");
                }
              }}
              placeholder="DeepLX token"
              className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={handleDeepLXTest}
              disabled={testingDeepLX || !config.deeplx.api_key_configured}
              className="rounded-md border border-[hsl(var(--border))] px-3 py-1.5 text-xs font-mono hover:bg-[hsl(var(--muted))] transition-colors disabled:opacity-40"
            >
              {testingDeepLX ? "测试中…" : "测试翻译"}
            </button>
            {config.deeplx.api_key_configured &&
              config.credential_storage?.mode === "system" && (
                <button
                  onClick={() => handleDeleteCredential("deeplx")}
                  className="text-xs font-mono text-red-500 hover:underline"
                >
                  从系统凭据库删除 Key
                </button>
              )}
          </div>
        </div>
      </section>

      {/* MCP / 工具 servers */}
      <section
        hidden={section !== "tools"}
        className="border-b border-[hsl(var(--border))] pb-10"
      >
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-medium tracking-tight">MCP / 工具 servers</h2>
          </div>
          <button
            onClick={addMcpServer}
            className="rounded-md border border-dashed border-[hsl(var(--border))] px-3 py-1.5 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
          >
            + 新增 server
          </button>
        </div>

        {config.mcp_servers.length === 0 ? (
          <button
            onClick={addMcpServer}
            className="w-full rounded-md border border-dashed border-[hsl(var(--border))] px-3 py-4 text-left text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
          >
            暂无 MCP server
          </button>
        ) : (
          <div className="space-y-4">
            {config.mcp_servers.map((server, idx) => (
              <div
                key={`${server.name}-${idx}`}
                className="rounded-md border border-[hsl(var(--border))] p-4"
              >
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                  <label className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    <input
                      type="checkbox"
                      checked={server.enabled}
                      onChange={(e) => updateMcpServer(idx, { enabled: e.target.checked })}
                      className="h-4 w-4 rounded border-[hsl(var(--border))]"
                    />
                    启用
                  </label>
                  <button
                    onClick={() => removeMcpServer(idx)}
                    className="text-xs font-mono text-red-500 hover:underline"
                  >
                    删除
                  </button>
                </div>

                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                      名称
                    </label>
                    <input
                      type="text"
                      value={server.name}
                      onChange={(e) => updateMcpServer(idx, { name: e.target.value })}
                      placeholder="paper-search"
                      className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                      Transport
                    </label>
                    <select
                      value={server.transport}
                      onChange={(e) =>
                        updateMcpServer(idx, {
                          transport: e.target.value as MCPServerConfig["transport"],
                        })
                      }
                      className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                    >
                      {MCP_TRANSPORTS.map((transport) => (
                        <option key={transport} value={transport}>
                          {transport}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                {server.transport === "stdio" ? (
                  <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
                    <div>
                      <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                        Command
                      </label>
                      <input
                        type="text"
                        value={server.command}
                        onChange={(e) => updateMcpServer(idx, { command: e.target.value })}
                        placeholder="npx"
                        className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                        Args
                      </label>
                      <input
                        type="text"
                        value={server.args.join(" ")}
                        onChange={(e) =>
                          updateMcpServer(idx, {
                            args: e.target.value.split(/\s+/).filter(Boolean),
                          })
                        }
                        placeholder="@modelcontextprotocol/server"
                        className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                      />
                    </div>
                  </div>
                ) : (
                  <div className="mt-4">
                    <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                      URL
                    </label>
                    <input
                      type="text"
                      value={server.url || ""}
                      onChange={(e) => updateMcpServer(idx, { url: e.target.value || null })}
                      placeholder="http://127.0.0.1:9000/mcp"
                      className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                    />
                  </div>
                )}

                <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                      Tool name
                    </label>
                    <input
                      type="text"
                      value={server.tool_name}
                      onChange={(e) => updateMcpServer(idx, { tool_name: e.target.value })}
                      placeholder="paper_search"
                      className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                      Timeout seconds
                    </label>
                    <input
                      type="number"
                      min={1}
                      max={60}
                      step={1}
                      value={server.timeout_seconds}
                      onChange={(e) =>
                        updateMcpServer(idx, {
                          timeout_seconds: Number(e.target.value) || 12,
                        })
                      }
                      className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                    />
                  </div>
                </div>

                <div className="mt-4">
                  <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                    Allowed tools
                  </label>
                  <input
                    type="text"
                    value={(server.allowed_tools || []).join(", ")}
                    onChange={(e) =>
                      updateMcpServer(idx, {
                        allowed_tools: e.target.value
                          .split(",")
                          .map((item) => item.trim())
                          .filter(Boolean),
                      })
                    }
                    placeholder="留空 = 暴露全部 discovery 工具"
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                  />
                </div>

                <div className="mt-4">
                  <p className="mb-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                    权限范围
                  </p>
                  <div className="flex flex-wrap gap-3">
                    {MCP_PERMISSION_OPTIONS.map((option) => (
                      <label
                        key={option.value}
                        className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]"
                      >
                        <input
                          type="checkbox"
                          checked={server.permission_scopes.includes(option.value)}
                          onChange={(e) =>
                            toggleMcpPermission(idx, option.value, e.target.checked)
                          }
                          className="h-4 w-4 rounded border-[hsl(var(--border))]"
                        />
                        {option.label}
                      </label>
                    ))}
                  </div>
                </div>

                <div className="mt-4 flex flex-wrap items-start gap-3">
                  <button
                    onClick={() => testMcpConnection(idx)}
                    disabled={mcpTests[idx]?.loading}
                    className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors disabled:opacity-50"
                  >
                    {mcpTests[idx]?.loading ? "测试中…" : "测试连接"}
                  </button>
                  {mcpTests[idx] && (
                    <p
                      className={`flex-1 min-w-[12rem] text-xs font-mono leading-5 ${
                        mcpTests[idx].loading
                          ? "text-[hsl(var(--muted-foreground))]"
                          : mcpTests[idx].ok
                            ? "text-green-600"
                            : "text-red-500"
                      }`}
                    >
                      {mcpTests[idx].message}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* MinerU 文档解析 */}
      <section hidden={section !== "tools"}>
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">MinerU 文档解析</h2>
        </div>

        <div className="space-y-5">
          <div className="rounded-md border border-[hsl(var(--border))] p-4">
            <div className="mb-4 flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                <input
                  type="checkbox"
                  checked={config.mineru.enabled}
                  onChange={(e) => updateMineru({ enabled: e.target.checked })}
                  className="h-4 w-4 rounded border-[hsl(var(--border))]"
                />
                启用 MinerU
              </label>
              {config.mineru.api_token_configured && (
                <span className="text-xs font-mono text-green-600">API token 已配置</span>
              )}
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  Base URL
                </label>
                <input
                  type="text"
                  value={config.mineru.base_url}
                  onChange={(e) => updateMineru({ base_url: e.target.value })}
                  placeholder="https://mineru.net"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  模式
                </label>
                <select
                  value={config.mineru.mode}
                  onChange={(e) =>
                    updateMineru({ mode: e.target.value as AppConfigData["mineru"]["mode"] })
                  }
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                >
                  {MINERU_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </select>
              </div>
              <div className="sm:col-span-2">
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  API Token
                </label>
                <input
                  type="password"
                  value={mineruTokenInput}
                  onChange={(e) => setMineruTokenInput(e.target.value)}
                  onFocus={() => {
                    if (isSecretPlaceholder(mineruTokenInput)) {
                      setMineruTokenInput("");
                    }
                  }}
                  placeholder="Bearer token"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
                {config.mineru.api_token_configured &&
                  config.credential_storage?.mode === "system" && (
                    <button
                      onClick={() => handleDeleteCredential("mineru")}
                      className="mt-2 text-xs font-mono text-red-500 hover:underline"
                    >
                      从系统凭据库删除 Token
                    </button>
                  )}
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  默认语言
                </label>
                <select
                  value={config.mineru.language}
                  onChange={(e) => updateMineru({ language: e.target.value })}
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                >
                  {MINERU_LANGUAGES.map((language) => (
                    <option key={language} value={language}>
                      {language}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  默认页码范围
                </label>
                <input
                  type="text"
                  value={config.mineru.page_range || ""}
                  onChange={(e) => updateMineru({ page_range: e.target.value || null })}
                  placeholder="1-10"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
            </div>

            <div className="mt-4 flex flex-wrap gap-4">
              <label className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                <input
                  type="checkbox"
                  checked={config.mineru.enable_table}
                  onChange={(e) => updateMineru({ enable_table: e.target.checked })}
                  className="h-4 w-4 rounded border-[hsl(var(--border))]"
                />
                表格
              </label>
              <label className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                <input
                  type="checkbox"
                  checked={config.mineru.enable_formula}
                  onChange={(e) => updateMineru({ enable_formula: e.target.checked })}
                  className="h-4 w-4 rounded border-[hsl(var(--border))]"
                />
                公式
              </label>
              <label className="flex items-center gap-2 text-xs font-mono text-[hsl(var(--muted-foreground))]">
                <input
                  type="checkbox"
                  checked={config.mineru.is_ocr}
                  onChange={(e) => updateMineru({ is_ocr: e.target.checked })}
                  className="h-4 w-4 rounded border-[hsl(var(--border))]"
                />
                OCR
              </label>
            </div>
          </div>

          <div className="rounded-md border border-[hsl(var(--border))] p-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,0.7fr)]">
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  PDF URL
                </label>
                <input
                  type="url"
                  value={mineruParseForm.url}
                  onChange={(e) =>
                    setMineruParseForm({ ...mineruParseForm, url: e.target.value })
                  }
                  placeholder="https://example.com/paper.pdf"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  文件名
                </label>
                <input
                  type="text"
                  value={mineruParseForm.file_name}
                  onChange={(e) =>
                    setMineruParseForm({ ...mineruParseForm, file_name: e.target.value })
                  }
                  placeholder="paper.pdf"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
              <div>
                <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                  标题
                </label>
                <input
                  type="text"
                  value={mineruParseForm.title}
                  onChange={(e) =>
                    setMineruParseForm({ ...mineruParseForm, title: e.target.value })
                  }
                  placeholder="留空时使用解析标题"
                  className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                    页码
                  </label>
                  <input
                    type="text"
                    value={mineruParseForm.page_range}
                    onChange={(e) =>
                      setMineruParseForm({ ...mineruParseForm, page_range: e.target.value })
                    }
                    placeholder="1-2"
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                  />
                </div>
                <div>
                  <label className="block text-xs font-mono text-[hsl(var(--muted-foreground))] mb-1.5">
                    语言
                  </label>
                  <select
                    value={mineruParseForm.language}
                    onChange={(e) =>
                      setMineruParseForm({ ...mineruParseForm, language: e.target.value })
                    }
                    className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
                  >
                    {MINERU_LANGUAGES.map((language) => (
                      <option key={language} value={language}>
                        {language}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                onClick={handleMineruParse}
                disabled={mineruParsing || !config.mineru.enabled}
                className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-mono text-[hsl(var(--primary-foreground))] hover:opacity-90 transition-opacity disabled:opacity-40"
              >
                {mineruParsing ? "解析中…" : "解析 URL"}
              </button>
              {mineruParseMessage && (
                <span
                  className={`text-xs font-mono ${mineruParseMessage.type === "ok" ? "text-green-600" : "text-red-500"}`}
                >
                  {mineruParseMessage.text}
                </span>
              )}
            </div>
          </div>
        </div>
      </section>

      {/* 任务模型分配 */}
      <section hidden={section !== "models"}>
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">任务模型分配</h2>
        </div>
        <div className="space-y-3">
          {TASK_LABELS.map(({ key, label, placeholder }) => (
            <div key={key} className="flex items-center gap-4">
              <label className="w-32 text-xs font-mono text-[hsl(var(--muted-foreground))] shrink-0">
                {label}
              </label>
              <ModelSelect
                value={config.task_models[key] ?? ""}
                options={allModelOptions}
                placeholder={placeholder}
                onChange={(m) => updateTaskModel(key, m)}
              />
            </div>
          ))}
        </div>
      </section>

      {/* LiteLLM 翻译兼容 Prompt */}
      <section
        hidden={section !== "advanced"}
        className="border-b border-[hsl(var(--border))] pb-10"
      >
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">LiteLLM 翻译兼容 Prompt</h2>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <label className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
              系统提示词
            </label>
            <button
              onClick={() => setConfig({ ...config, translation_prompt: "" })}
              className="rounded-md border border-[hsl(var(--border))] px-2.5 py-1 text-xs font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
            >
              使用内置默认
            </button>
          </div>
          <textarea
            value={config.translation_prompt || ""}
            onChange={(e) =>
              setConfig({ ...config, translation_prompt: e.target.value })
            }
            placeholder={DEFAULT_TRANSLATION_PROMPT}
            rows={10}
            className="w-full resize-y rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-2 text-sm font-mono leading-relaxed focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
          />
          <p className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
            网页论文默认由 DeepLX 翻译，不使用此提示词；仅显式切回
            LiteLLM 兼容模式时生效，并自动传入上一段、当前段和下一段。
          </p>
        </div>
      </section>

      {/* 运行参数 */}
      <section hidden={section !== "advanced"}>
        <div className="mb-4">
          <h2 className="text-lg font-medium tracking-tight">运行参数</h2>
        </div>
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <label className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
              并发数
            </label>
            <input
              type="number"
              min={1}
              max={20}
              value={config.translation_concurrency}
              onChange={(e) =>
                setConfig({
                  ...config,
                  translation_concurrency: parseInt(e.target.value) || 5,
                })
              }
              className="w-16 rounded-md border border-[hsl(var(--border))] bg-transparent px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
              超时
            </label>
            <input
              type="number"
              min={10}
              max={600}
              value={config.request_timeout}
              onChange={(e) =>
                setConfig({
                  ...config,
                  request_timeout: parseFloat(e.target.value) || 120,
                })
              }
              className="w-20 rounded-md border border-[hsl(var(--border))] bg-transparent px-2 py-1 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))]"
            />
            <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">秒</span>
          </div>
        </div>
      </section>

      {/* 保存按钮 + 消息 */}
      <div className="flex items-center gap-4 pt-4 border-t border-[hsl(var(--border))]">
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-[hsl(var(--primary))] px-4 py-2 text-sm font-medium text-[hsl(var(--primary-foreground))] hover:opacity-90 transition-opacity disabled:opacity-40 font-mono"
        >
          {saving ? "保存中…" : "保存配置"}
        </button>
        {message && (
          <span
            className={`text-xs font-mono ${message.type === "ok" ? "text-green-600" : "text-red-500"}`}
          >
            {message.text}
          </span>
        )}
      </div>
    </div>
  );
}

/** 模型选择器 — dropdown + 可手输（照 ccswitch ModelInputWithFetch 三态模式）。 */
function ModelSelect({
  value,
  options,
  onChange,
  placeholder,
}: {
  value: string;
  options: string[];
  onChange: (model: string) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative flex-1 max-w-xs">
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
        className="w-full rounded-md border border-[hsl(var(--border))] bg-transparent px-3 py-1.5 text-sm font-mono pr-8 focus:outline-none focus:ring-1 focus:ring-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))]/60"
      />
      {options.length > 0 && (
        <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[hsl(var(--muted-foreground))] pointer-events-none">
          ▾
        </span>
      )}
      {open && options.length > 0 && (
        <div className="absolute z-50 mt-1 w-full rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] shadow-md max-h-48 overflow-y-auto">
          {options.map((opt) => (
            <button
              key={opt}
              onMouseDown={(e) => {
                e.preventDefault();
                onChange(opt);
                setOpen(false);
              }}
              className={`block w-full text-left px-3 py-1.5 text-xs font-mono hover:bg-[hsl(var(--muted))] transition-colors ${
                opt === value ? "bg-[hsl(var(--muted))]/50" : ""
              }`}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
