"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelPdfExport,
  createPdfExport,
  getPdfExportCapability,
  getPdfExportRun,
  listPdfExports,
  originalPdfDownloadUrl,
  pdfExportFailureMessage,
  pdfExportNoticeUrl,
  pdfExportSourceUrl,
  pdfExportUnavailableMessage,
  PdfExportRequestError,
  translatedPdfDownloadUrl,
  type PdfExportCapability,
  type PdfExportRun,
} from "@/lib/api";

const POLL_INTERVAL_MS = 1_500;
const POLL_FAILURE_NOTICE_THRESHOLD = 3;
const ADMIN_TOKEN_STORAGE_KEY = "peinidu_admin_token";

function storedAdminToken(): string {
  try {
    return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)?.trim() ?? "";
  } catch {
    return "";
  }
}

function isActive(run: PdfExportRun | null): run is PdfExportRun {
  return run?.status === "queued" || run?.status === "running";
}

function newestRelevantRun(runs: PdfExportRun[]): PdfExportRun | null {
  const ordered = [...runs].sort((left, right) => {
    const leftTime = Date.parse(left.updated_at || left.created_at) || 0;
    const rightTime = Date.parse(right.updated_at || right.created_at) || 0;
    return rightTime - leftTime;
  });
  return ordered.find(isActive) ?? ordered[0] ?? null;
}

function normalizedProgress(run: PdfExportRun): number | null {
  if (
    typeof run.progress !== "number"
    || !Number.isFinite(run.progress)
    || run.progress < 0
    || run.progress > 1
  ) {
    return null;
  }
  return run.progress;
}

function runningLabel(run: PdfExportRun): string {
  if (run.status === "queued") return "中文 PDF 已排队";
  const pageCount = run.page_count ?? run.source_pages ?? 0;
  const pagesDone = run.pages_done ?? 0;
  if (pageCount > 0 && pagesDone > 0) {
    return `正在生成中文 PDF，已完成 ${pagesDone}/${pageCount} 页`;
  }
  const progress = normalizedProgress(run);
  if (progress === null) return "正在生成中文 PDF，进度暂不可用";
  const percentage = Math.round(progress * 100);
  return percentage > 0
    ? `正在生成中文 PDF，已完成 ${percentage}%`
    : "正在生成中文 PDF";
}

function capabilityFacts(capability: PdfExportCapability) {
  const sidecar = capability.sidecar;
  const wrapperVersion = sidecar.wrapper_version || capability.wrapper_version;
  return [
    sidecar.license ? ["许可证", sidecar.license] : null,
    wrapperVersion ? ["Pet 适配器版本", wrapperVersion] : null,
    sidecar.version ? ["版本", sidecar.version] : null,
    sidecar.commit ? ["提交", sidecar.commit] : null,
    sidecar.image_digest ? ["镜像摘要", sidecar.image_digest] : null,
  ].filter((item): item is string[] => item !== null);
}

function CapabilityDisclosure({ capability }: { capability: PdfExportCapability }) {
  const facts = capabilityFacts(capability);
  const sourceUrl = capability.sidecar.source_code_url || capability.source_url || null;
  const modifiedSourceUrl = capability.sidecar.modified_source_url
    || capability.modified_source_url
    || null;
  return (
    <details className="reader-pdf-export-details">
      <summary className="reader-secondary-action">
        {capability.enabled ? "导出说明" : "中文 PDF 未启用"}
      </summary>
      <div className="reader-pdf-export-disclosure">
        <strong>部署与许可证说明</strong>
        <p>
          {capability.enabled
            ? "中文 PDF 由独立服务生成，输出为单语中文文件；原始 PDF 始终单独保留。"
            : pdfExportUnavailableMessage(capability.reason || capability.error_code)}
        </p>
        {facts.length > 0 && (
          <dl>
            {facts.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
        <div className="reader-pdf-export-source-links">
          {capability.notice_url && (
            <a href={pdfExportNoticeUrl(capability.notice_url)} target="_blank" rel="noreferrer">
              查看完整第三方声明
            </a>
          )}
          {sourceUrl && (
            <a href={sourceUrl} target="_blank" rel="noreferrer">
              查看上游源码
            </a>
          )}
          {modifiedSourceUrl && modifiedSourceUrl !== sourceUrl && (
            <a href={pdfExportSourceUrl(modifiedSourceUrl)} target="_blank" rel="noreferrer">
              查看部署修改源码
            </a>
          )}
        </div>
      </div>
    </details>
  );
}

export function PdfExportControl({
  paperId,
  readerReady,
}: {
  paperId: string;
  readerReady: boolean;
}) {
  const [capability, setCapability] = useState<PdfExportCapability | null>(null);
  const [run, setRun] = useState<PdfExportRun | null>(null);
  const [capabilityLoading, setCapabilityLoading] = useState(false);
  const [runsLoading, setRunsLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [runListError, setRunListError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const pollFailureCountRef = useRef(0);

  const originalUrl = originalPdfDownloadUrl(paperId);
  const visibleRun = run?.arxiv_id === paperId ? run : null;
  const canCreate = capability?.enabled === true
    && !capabilityLoading
    && !capabilityError
    && !runsLoading
    && !runListError;

  useEffect(() => {
    if (!readerReady) return;
    let stopped = false;
    setCapabilityLoading(true);
    setRunsLoading(true);
    setCapabilityError(null);
    setRunListError(null);
    setActionError(null);
    setPollError(null);
    pollFailureCountRef.current = 0;
    void getPdfExportCapability()
      .then((nextCapability) => {
        if (stopped) return;
        setCapability(nextCapability);
      })
      .catch(() => {
        if (!stopped) {
          setCapability(null);
          setCapabilityError("暂时无法确认是否可以新建中文 PDF；已有任务和文件仍可正常查看。");
        }
      })
      .finally(() => {
        if (!stopped) setCapabilityLoading(false);
      });
    void listPdfExports(paperId)
      .then((runs) => {
        if (stopped) return;
        setRun(newestRelevantRun(runs));
      })
      .catch(() => {
        if (!stopped) {
          setRunListError("中文 PDF 任务状态暂时无法读取，请稍后重新检查。");
        }
      })
      .finally(() => {
        if (!stopped) setRunsLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, [paperId, readerReady, reloadToken]);

  useEffect(() => {
    if (!readerReady || !isActive(visibleRun) || cancelling) return;
    let stopped = false;
    let timer: number | null = null;

    const schedule = () => {
      if (!stopped) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };
    const poll = async () => {
      try {
        const nextRun = await getPdfExportRun(paperId, visibleRun.id);
        if (stopped) return;
        pollFailureCountRef.current = 0;
        setPollError(null);
        setRun(nextRun);
        if (isActive(nextRun)) schedule();
      } catch {
        if (stopped) return;
        pollFailureCountRef.current += 1;
        if (pollFailureCountRef.current >= POLL_FAILURE_NOTICE_THRESHOLD) {
          setPollError("进度更新暂时中断，正在继续重试。生成任务不会因此取消。");
        }
        schedule();
      }
    };

    schedule();
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [cancelling, paperId, readerReady, visibleRun]);

  const createRun = useCallback(async () => {
    if (creating) return;
    setCreating(true);
    setActionError(null);
    setPollError(null);
    pollFailureCountRef.current = 0;
    try {
      setRun(await createPdfExport(paperId, storedAdminToken()));
    } catch (error) {
      setActionError(error instanceof PdfExportRequestError
        ? pdfExportFailureMessage(error.code, error.message)
        : "中文 PDF 任务未能创建，请检查服务状态后重试。");
    } finally {
      setCreating(false);
    }
  }, [creating, paperId]);

  const cancelRun = useCallback(async () => {
    if (!visibleRun || cancelling) return;
    setCancelling(true);
    setActionError(null);
    try {
      setRun(await cancelPdfExport(paperId, visibleRun.id, storedAdminToken()));
    } catch (error) {
      setActionError(error instanceof PdfExportRequestError
        ? pdfExportFailureMessage(error.code, error.message)
        : "暂时无法取消中文 PDF 任务，请稍后再试。");
    } finally {
      setCancelling(false);
    }
  }, [cancelling, paperId, visibleRun]);

  const exportAction = (() => {
    if (!readerReady) return null;
    if (!visibleRun) {
      if (runsLoading || capabilityLoading) {
        return <span className="reader-pdf-export-status">正在检查中文 PDF…</span>;
      }
      if (runListError || capabilityError || !capability) return null;
      if (!capability.enabled) return null;
      return (
        <button
          type="button"
          className="reader-primary-action"
          disabled={creating}
          onClick={() => void createRun()}
        >
          {creating ? "正在创建…" : "生成中文 PDF"}
        </button>
      );
    }
    if (visibleRun.status === "queued" || visibleRun.status === "running") {
      const progress = normalizedProgress(visibleRun);
      return (
        <>
          <div className="reader-pdf-export-progress" aria-live="polite">
            <span>{runningLabel(visibleRun)}</span>
            <progress
              value={progress ?? undefined}
              max={1}
              aria-label={progress === null
                ? "中文 PDF 正在生成，进度暂不可用"
                : `中文 PDF 生成进度 ${Math.round(progress * 100)}%`}
            />
          </div>
          <button
            type="button"
            className="reader-secondary-action"
            disabled={cancelling}
            onClick={() => void cancelRun()}
          >
            {cancelling ? "正在取消…" : "取消生成"}
          </button>
        </>
      );
    }
    if (visibleRun.status === "done") {
      return (
        <>
          <span className="reader-pdf-export-status" aria-live="polite">中文 PDF 已生成</span>
          <a
            className="reader-primary-action reader-download-action"
            href={translatedPdfDownloadUrl(paperId, visibleRun.id)}
            download
          >
            下载中文 PDF
          </a>
        </>
      );
    }
    if (visibleRun.status === "error") {
      return (
        <>
          <span className="reader-pdf-export-failure" role="alert">
            {pdfExportFailureMessage(visibleRun.error_code, visibleRun.error_message)}
          </span>
          {canCreate ? (
            <button
              type="button"
              className="reader-secondary-action"
              disabled={creating}
              onClick={() => void createRun()}
            >
              {creating ? "正在重试…" : "重试生成"}
            </button>
          ) : capability && !capability.enabled ? (
            <span className="reader-pdf-export-status">当前部署不能新建中文 PDF 任务</span>
          ) : null}
        </>
      );
    }
    return (
      <>
        <span className="reader-pdf-export-status" aria-live="polite">中文 PDF 生成已取消</span>
        {canCreate ? (
          <button
            type="button"
            className="reader-secondary-action"
            disabled={creating}
            onClick={() => void createRun()}
          >
            {creating ? "正在创建…" : "重新生成"}
          </button>
        ) : capability && !capability.enabled ? (
          <span className="reader-pdf-export-status">当前部署不能新建中文 PDF 任务</span>
        ) : null}
      </>
    );
  })();

  return (
    <div className="reader-pdf-export-control" data-pdf-export-status={visibleRun?.status ?? "idle"}>
      <a className="reader-secondary-action reader-download-action" href={originalUrl} download>
        下载原始 PDF
      </a>
      {exportAction}
      {(capabilityError || runListError) && (
        <button
          type="button"
          className="reader-secondary-action"
          onClick={() => setReloadToken((value) => value + 1)}
        >
          重新检查中文 PDF
        </button>
      )}
      {capability && <CapabilityDisclosure capability={capability} />}
      {capabilityError && (
        <span className="reader-pdf-export-failure" role="alert">{capabilityError}</span>
      )}
      {runListError && (
        <span className="reader-pdf-export-failure" role="alert">{runListError}</span>
      )}
      {actionError && <span className="reader-pdf-export-failure" role="alert">{actionError}</span>}
      {pollError && <span className="reader-pdf-export-failure" role="alert">{pollError}</span>}
    </div>
  );
}
