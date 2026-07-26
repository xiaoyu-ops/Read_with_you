# PDF export third-party notice

Pet's optional PDF export sidecar uses the unmodified upstream packages below
inside a hardened derived image. The Pet adapter does not copy either
upstream project's internal representation into the web reader.

## PDFMathTranslate-next

- Version: 2.9.0, revision `f8dffcf4c3a33b254391d43514439b975ce8d966`
- Base image: `awwaawwa/pdfmathtranslate-next@sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a`
- Corresponding source: [PDFMathTranslate-next v2.9.0](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0)
- License: [GNU Affero General Public License v3.0 (AGPL-3.0)](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/v2.9.0/LICENSE)

## BabelDOC

The pinned image contains BabelDOC 0.6.2, the document translation engine used
by PDFMathTranslate-next.

- Corresponding source: [BabelDOC v0.6.2](https://github.com/funstory-ai/BabelDOC/tree/v0.6.2)
- License: [GNU Affero General Public License v3.0 (AGPL-3.0)](https://github.com/funstory-ai/BabelDOC/blob/v0.6.2/LICENSE)

## Pet adapter wrapper

Version: `1.0.1`.

The derived-image Dockerfile, authenticated job API, cache bootstrap and
runtime probes are distributed with Pet at [`sidecar/pdf_export/`](./). The
deployment verifier is [`scripts/verify_pdf_export_sidecar.py`](../../scripts/verify_pdf_export_sidecar.py).
No upstream Provider credential is passed into this wrapper.

A running backend exposes the exact wrapper source as a deterministic,
allowlisted ZIP at `GET /api/pdf-exports/wrapper-source`. The archive uses a
fixed order, timestamp and permissions, rejects symbolic links, and excludes
environment files, Provider configuration, user data and caches.

After translation, the wrapper scans page links, widgets, the document TOC and
page annotations. Within that scope it restores only allowlisted links,
internal TOC entries and common annotations without actions, then normalizes
and validates the temporary PDF before atomic publication. A scanned
JavaScript URI, launch or external-file action, file annotation, external TOC
target, form, rich media or unsupported/action-bearing annotation fails the
whole export closed. HTTP(S) links are scheme-allowlisted, not hostname-
allowlisted.

The source, temporary output and normalized output also fail closed on the
enumerated catalog/page active-content boundaries: `/OpenAction`, `/AA`,
`/Names/JavaScript`, `EmbeddedFiles`, `/AF`, `AcroForm`, `Collection`,
Renditions, AlternatePresentations and PresSteps. Xref parsing uncertainty is
an export failure. Wrapper `1.0.1` also compares the rendered source and
translated image regions. If a non-page-sized image resource is retained but
covered by an opaque translation layer, the wrapper repaints that region from
the already-audited source page; page-sized scan images are excluded so OCR
translations are not hidden. Wrapper `1.0.1` is still not a general PDF sanitizer: these
enumerated checks must not be described as complete sanitization of every
current or future PDF extension.

The running wrapper reports a deterministic source hash. The backend compares
it with the same allowlisted source that it publishes at
`GET /api/pdf-exports/wrapper-source`; a mismatch disables new exports and
prevents completed-run cache reuse.

The sidecar is disabled by default and is not required for the web inline
reader. T13 runs it loopback-only on the developer MacBook; the current 3.8 GiB
VPS intentionally keeps it disabled. Any future public activation still
requires at least 8 GiB of memory and a new target-platform acceptance run.
Local completion is not a claim of public activation. If either upstream is
modified in the future, the corresponding modified source must be published
and this notice updated before deployment.
