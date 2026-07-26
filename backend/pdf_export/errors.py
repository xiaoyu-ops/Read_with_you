"""Stable error contract for PDF export Runs."""

from __future__ import annotations


class PdfExportError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


RETRYABLE_ERROR_CODES = {
    "export_timeout",
    "sidecar_rate_limited",
    "sidecar_unavailable",
    "sidecar_crashed",
    "backend_restarted",
}
