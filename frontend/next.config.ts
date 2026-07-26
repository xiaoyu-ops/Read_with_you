import type { NextConfig } from "next";

// 验证构建与运行中的 dev server 共用 .next 会互相踩脏缓存
// （症状：dev SSR 报 Cannot find module './vendor-chunks/xxx.js'，页面 500）。
// 验证时用 NEXT_DIST_DIR=.next-build npm run build 走独立目录；
// 不设置该变量时（本地 dev、Docker 构建）保持默认 .next，行为不变。
const nextConfig: NextConfig = {
  distDir: process.env.NEXT_DIST_DIR || ".next",
  output: "standalone",
  // A signed local Core must never create Next's image cache inside the
  // application bundle. The two product images are already optimized assets.
  images: {
    unoptimized: true,
  },
  async rewrites() {
    return [
      {
        source: "/pdfjs/pdf-5.6.205.min.js",
        destination: "/pdfjs/pdf.min.mjs",
      },
      {
        source: "/pdfjs/pdf.worker-5.6.205.min.js",
        destination: "/pdfjs/pdf.worker.min.mjs",
      },
    ];
  },
  async headers() {
    const pdfRuntimeHeaders = ["pdf-5.6.205.min.js", "pdf.worker-5.6.205.min.js"].map((fileName) => ({
      source: `/pdfjs/${fileName}`,
      headers: [
        {
          key: "Cache-Control",
          value: "public, max-age=31536000, s-maxage=31536000, immutable",
        },
      ],
    }));
    return [
      ...pdfRuntimeHeaders,
      {
        source: "/paper/:id",
        headers: [
          {
            key: "Link",
            value: [
              "</assets/:id/original.pdf>; rel=preload; as=fetch; crossorigin=anonymous; fetchpriority=high",
              "</api/papers/:id>; rel=preload; as=fetch; crossorigin=anonymous",
              "</api/papers/:id/translation-layout?build=false>; rel=preload; as=fetch; crossorigin=anonymous",
            ].join(", "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
