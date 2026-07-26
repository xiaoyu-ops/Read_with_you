import type { ReactNode } from "react";

import { PaperPdfPreloadBridge } from "@/components/PaperPdfPreloadBridge";


function apiHref(path: string): string {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";
  return `${apiBase.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

function assetHref(path: string): string {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";
  if (apiBase.startsWith("/")) return path;
  return new URL(path, new URL(apiBase).origin).toString();
}

export default async function PaperLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const paperPath = `papers/${encodeURIComponent(id)}`;
  const pdfUrl = assetHref(`/assets/${encodeURIComponent(id)}/original.pdf`);
  const preloadConfig = JSON.stringify({
    paperId: id,
    pdfUrl,
    moduleUrl: "/pdfjs/pdf-5.6.205.min.js",
    workerUrl: "/pdfjs/pdf.worker-5.6.205.min.js",
    debug: process.env.NODE_ENV !== "production",
  }).replace(/</g, "\\u003c");

  return (
    <>
      <link
        rel="preload"
        as="fetch"
        href={pdfUrl}
        crossOrigin="anonymous"
        fetchPriority="high"
      />
      <link
        rel="preload"
        as="fetch"
        href={apiHref(paperPath)}
        crossOrigin="anonymous"
      />
      <link
        rel="preload"
        as="fetch"
        href={apiHref(`${paperPath}/translation-layout?build=false`)}
        crossOrigin="anonymous"
      />
      <link rel="modulepreload" href="/pdfjs/pdf-5.6.205.min.js" />
      <link rel="modulepreload" href="/pdfjs/pdf.worker-5.6.205.min.js" />
      <PaperPdfPreloadBridge paperId={id} pdfUrl={pdfUrl} />
      <script
        dangerouslySetInnerHTML={{
          __html: `
            {
              const config = ${preloadConfig};
              const current = globalThis.__petPdfDocumentPreload;
              if (
                !current ||
                current.paperId !== config.paperId ||
                current.url !== config.pdfUrl
              ) {
                const preload = {
                  paperId: config.paperId,
                  url: config.pdfUrl,
                  promise: null,
                  consumers: 0,
                };
                preload.promise = import(config.moduleUrl).then(async (pdfjs) => {
                  pdfjs.GlobalWorkerOptions.workerSrc = config.workerUrl;
                  if (config.debug) {
                    const loadUrl = new URL(config.pdfUrl, window.location.href).toString();
                    const loadCounts = globalThis.__petPdfDocumentLoadCounts ?? {};
                    loadCounts[loadUrl] = (loadCounts[loadUrl] ?? 0) + 1;
                    globalThis.__petPdfDocumentLoadCounts = loadCounts;
                    const loadTrace = globalThis.__petPdfDocumentLoadTrace ?? [];
                    loadTrace.push({ url: loadUrl, source: "server_layout" });
                    globalThis.__petPdfDocumentLoadTrace = loadTrace;
                  }
                  const loadingTask = pdfjs.getDocument({ url: config.pdfUrl });
                  preload.loadingTask = loadingTask;
                  const document = await loadingTask.promise;
                  return { document, loadingTask, pdfjs };
                });
                globalThis.__petPdfDocumentPreload = preload;
                preload.cleanupTimer = window.setTimeout(() => {
                  if (
                    globalThis.__petPdfDocumentPreload !== preload ||
                    preload.consumers > 0
                  ) return;
                  delete globalThis.__petPdfDocumentPreload;
                  preload.promise
                    .then(({ document }) => document.destroy())
                    .catch(() => preload.loadingTask?.destroy())
                    .catch(() => undefined);
                }, 120000);
                preload.promise.catch(async () => {
                  if (config.debug) {
                    const events = globalThis.__petPdfDocumentPreloadTrace ?? [];
                    events.push({ event: "server_layout_rejected", url: config.pdfUrl });
                    globalThis.__petPdfDocumentPreloadTrace = events;
                  }
                  if (globalThis.__petPdfDocumentPreload === preload) {
                    delete globalThis.__petPdfDocumentPreload;
                  }
                  try {
                    await preload.loadingTask?.destroy();
                  } catch {}
                });
              }
            }
          `,
        }}
      />
      {children}
    </>
  );
}
