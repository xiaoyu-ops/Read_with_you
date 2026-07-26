"""Isolated PDFMathTranslate-next job API used by Pet's backend.

Only the monolingual, watermarked Chinese PDF is retained.  Upstream provider
credentials never enter this container; PDFMathTranslate calls Pet's private
OpenAI-compatible LiteLLM route with a fixed model alias instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import math
import os
import re
import shutil
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

try:
    import fitz
    from pdf2zh_next.config.model import (
        BasicSettings,
        PDFSettings,
        SettingsModel,
        TranslationSettings,
    )
    from pdf2zh_next.config.translate_engine_model import OpenAISettings
    from pdf2zh_next.high_level import do_translate_async_stream

    _UPSTREAM_IMPORT_ERROR: str | None = None
except ImportError as error:  # Allows host-side unit tests without the pinned image.
    fitz = None  # type: ignore[assignment]
    BasicSettings = PDFSettings = SettingsModel = TranslationSettings = None  # type: ignore[assignment]
    OpenAISettings = None  # type: ignore[assignment]
    do_translate_async_stream = None  # type: ignore[assignment]
    _UPSTREAM_IMPORT_ERROR = error.__class__.__name__


app = FastAPI(title="Pet PDF export sidecar", docs_url=None, redoc_url=None)

UPSTREAM_VERSION = "2.9.0"
UPSTREAM_REVISION = "f8dffcf4c3a33b254391d43514439b975ce8d966"
UPSTREAM_IMAGE = (
    "awwaawwa/pdfmathtranslate-next@"
    "sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a"
)
UPSTREAM_SOURCE = "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0"
UPSTREAM_LICENSE = "AGPL-3.0"
WRAPPER_VERSION = "1.0.1"
MODEL_ALIAS = "pdf-translation"

WRAPPER_SOURCE_FILES = (
    "app.py",
    "Dockerfile",
    "entrypoint.sh",
    "healthcheck.py",
    "runtime_probe.py",
    "README.md",
    "THIRD_PARTY.md",
    "tests/__init__.py",
    "tests/test_app.py",
)

_UNSAFE_CATALOG_KEYS = frozenset(
    {
        "AA",
        "AcroForm",
        "AF",
        "Collection",
        "EmbeddedFiles",
        "JavaScript",
        "OpenAction",
    }
)
_UNSAFE_PAGE_KEYS = frozenset(
    {
        "AA",
        "AF",
        "EmbeddedFiles",
        "JavaScript",
        "OpenAction",
        "PresSteps",
    }
)
_UNSAFE_NAME_TREE_KEYS = (
    "JavaScript",
    "EmbeddedFiles",
    "Renditions",
    "AlternatePresentations",
)

WORK_ROOT = Path(os.environ.get("PEINIDU_PDF_EXPORT_WORK_ROOT", "/work/jobs"))
MAX_FILE_BYTES = max(1, int(os.environ.get("PEINIDU_PDF_EXPORT_MAX_FILE_BYTES", 50 * 1024 * 1024)))
MAX_PAGES = max(1, int(os.environ.get("PEINIDU_PDF_EXPORT_MAX_PAGES", "200")))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("PEINIDU_PDF_EXPORT_CONCURRENCY", "1")))
MAX_JOB_SECONDS = max(30, int(os.environ.get("PEINIDU_PDF_EXPORT_TIMEOUT_SECONDS", "1800")))
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TERMINAL_STATES = frozenset({"done", "error", "cancelled"})


class OutputValidationError(RuntimeError):
    """The source or translated PDF fails the export safety contract."""


@dataclass(slots=True)
class ExportJob:
    job_id: str
    directory: Path
    input_path: Path
    output_path: Path
    page_count: int
    status: str = "queued"
    progress: float = 0.0
    stage: str = "queued"
    error_code: str | None = None
    error_message: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": round(max(0.0, min(1.0, self.progress)), 4),
            "stage": self.stage,
            "page_count": self.page_count,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
            "download_ready": self.status == "done" and self.output_path.is_file(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_jobs: dict[str, ExportJob] = {}
_jobs_lock = asyncio.Lock()


def _wrapper_source_sha256(root: Path | None = None) -> str:
    """Hash the public in-container wrapper sources with unambiguous framing."""
    source_root = root or Path(__file__).parent
    if source_root.is_symlink():
        raise OSError("wrapper source root must not be a symbolic link")
    resolved_root = source_root.resolve(strict=True)
    digest = hashlib.sha256()
    for relative_name in WRAPPER_SOURCE_FILES:
        relative_path = Path(relative_name)
        current = source_root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise OSError("wrapper source must not contain a symbolic link")
        source = (source_root / relative_path).resolve(strict=True)
        if (
            not source.is_relative_to(resolved_root)
            or not stat.S_ISREG(source.stat().st_mode)
        ):
            raise OSError("wrapper source file is unavailable")
        path_bytes = relative_name.encode("utf-8")
        content = source.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _token() -> str:
    return os.environ.get("PEINIDU_PDF_EXPORT_INTERNAL_TOKEN", "").strip()


async def _require_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = _token()
    if not expected:
        raise HTTPException(status_code=503, detail="PDF export sidecar is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid PDF export bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _job_directory(job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=422, detail="Invalid job_id")
    root = WORK_ROOT.resolve()
    target = (root / job_id).resolve()
    if target.parent != root:
        raise HTTPException(status_code=422, detail="Invalid job_id")
    return target


def _require_job(job_id: str) -> ExportJob:
    _job_directory(job_id)
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="PDF export job not found")
    return job


def _safe_error(error: BaseException | str) -> tuple[str, str]:
    if isinstance(error, OutputValidationError):
        return "output_validation_failed", "The translated PDF did not pass safety validation."
    text = str(error).lower()
    if any(marker in text for marker in ("authentication", "unauthorized", "api key", "status 401")):
        return "provider_authentication_failed", "The translation provider rejected its credentials."
    if any(marker in text for marker in ("rate limit", "too many requests", "status 429")):
        return "provider_rate_limited", "The translation provider is rate limited."
    if "timeout" in text or "timed out" in text:
        return "provider_timeout", "The translation provider timed out."
    return "translation_failed", "The PDF translation failed."


def _event_progress(event: dict[str, Any]) -> tuple[float | None, str | None]:
    stage = event.get("stage") or event.get("name") or event.get("type")
    safe_stage = str(stage)[:80] if stage is not None else None
    for key in ("overall_progress", "progress", "percent", "percentage"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1:
                numeric /= 100
            return max(0.0, min(1.0, numeric)), safe_stage
    return None, safe_stage


async def _save_upload(upload: UploadFile, path: Path) -> None:
    if upload.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only application/pdf uploads are accepted")
    size = 0
    header = b""
    try:
        with path.open("xb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds the export size limit")
                if not header:
                    header = chunk[:5]
                target.write(chunk)
    finally:
        await upload.close()
    if not header.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="Uploaded file is not a PDF")


def _validate_pdf(path: Path) -> int:
    if fitz is None:
        raise HTTPException(status_code=503, detail="PDF export runtime is unavailable")
    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise HTTPException(status_code=422, detail="Encrypted PDFs are not supported")
            pages = int(document.page_count)
            if pages < 1:
                raise HTTPException(status_code=422, detail="PDF has no pages")
            if pages > MAX_PAGES:
                raise HTTPException(status_code=413, detail="PDF exceeds the export page limit")
            _reject_unsafe_global_entries(document)
    except (HTTPException, OutputValidationError):
        raise
    except Exception as error:
        raise HTTPException(status_code=422, detail="Uploaded PDF could not be opened") from error
    return pages


def _point_tuple(value: Any) -> tuple[float, float]:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    point = fitz.Point(value)
    coordinates = (float(point.x), float(point.y))
    if not all(math.isfinite(item) for item in coordinates):
        raise OutputValidationError("PDF interactivity contains an invalid point")
    return coordinates


def _rect_tuple(value: Any) -> tuple[float, float, float, float]:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    rect = fitz.Rect(value)
    coordinates = tuple(float(item) for item in rect)
    if (
        not all(math.isfinite(item) for item in coordinates)
        or rect.is_empty
        or rect.is_infinite
    ):
        raise OutputValidationError("PDF interactivity contains an invalid rectangle")
    return coordinates  # type: ignore[return-value]


def _geometry_signature(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(round(item, 3) for item in values)


def _ensure_rect_on_page(
    rect: tuple[float, float, float, float],
    page_rect: Any,
) -> None:
    tolerance = 1.0
    if (
        rect[0] < float(page_rect.x0) - tolerance
        or rect[1] < float(page_rect.y0) - tolerance
        or rect[2] > float(page_rect.x1) + tolerance
        or rect[3] > float(page_rect.y1) + tolerance
    ):
        raise OutputValidationError("PDF interactivity lies outside its source page")


def _safe_uri(value: Any) -> str:
    uri = str(value or "").strip()
    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return uri
    if scheme == "mailto" and parsed.path:
        return uri
    raise OutputValidationError("PDF contains a disallowed URI action")


def _safe_link_spec(
    link: dict[str, Any],
    *,
    page_count: int,
    page_rect: Any,
    target_page_rects: list[Any],
    named_destinations: dict[str, Any],
) -> dict[str, Any]:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    kind = int(link.get("kind", fitz.LINK_NONE))
    source_rect = _rect_tuple(link.get("from"))
    _ensure_rect_on_page(source_rect, page_rect)
    if link.get("file"):
        raise OutputValidationError("PDF contains an external file action")

    if kind == fitz.LINK_URI:
        return {"kind": kind, "from": source_rect, "uri": _safe_uri(link.get("uri"))}
    if kind == fitz.LINK_NAMED:
        name = str(link.get("nameddest") or link.get("name") or "")
        if not name or len(name) > 512 or name not in named_destinations:
            raise OutputValidationError("PDF contains an unsafe or unresolved named action")
        return {"kind": kind, "from": source_rect, "nameddest": name}
    if kind != fitz.LINK_GOTO:
        raise OutputValidationError("PDF contains an unsupported or unsafe link action")

    target_page = link.get("page")
    if not isinstance(target_page, int) or not 0 <= target_page < page_count:
        raise OutputValidationError("PDF contains an invalid internal page target")
    target = _point_tuple(link.get("to") or (0, 0))
    target_rect = target_page_rects[target_page]
    tolerance = 1.0
    if (
        target[0] < float(target_rect.x0) - tolerance
        or target[1] < float(target_rect.y0) - tolerance
        or target[0] > float(target_rect.x1) + tolerance
        or target[1] > float(target_rect.y1) + tolerance
    ):
        raise OutputValidationError("PDF contains an invalid internal destination")
    spec: dict[str, Any] = {
        "kind": kind,
        "from": source_rect,
        "page": target_page,
        "to": target,
    }
    zoom = link.get("zoom")
    if isinstance(zoom, (int, float)) and math.isfinite(float(zoom)):
        spec["zoom"] = float(zoom)
    return spec


def _link_signature(spec: dict[str, Any]) -> tuple[Any, ...]:
    if spec["kind"] == fitz.LINK_URI:
        return (spec["kind"], _geometry_signature(spec["from"]), spec["uri"])
    if spec["kind"] == fitz.LINK_NAMED:
        return (spec["kind"], _geometry_signature(spec["from"]), spec["nameddest"])
    return (
        spec["kind"],
        _geometry_signature(spec["from"]),
        spec["page"],
        _geometry_signature(spec["to"]),
    )


def _annotation_action_is_present(document: Any, xref: int, key: str) -> bool:
    value_type, value = document.xref_get_key(xref, key)
    return value_type != "null" and value not in {"null", ""}


def _safe_annotation_spec(document: Any, page_rect: Any, annotation: Any) -> dict[str, Any]:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    annotation_type, type_name = annotation.type
    supported = {
        fitz.PDF_ANNOT_TEXT,
        fitz.PDF_ANNOT_FREE_TEXT,
        fitz.PDF_ANNOT_LINE,
        fitz.PDF_ANNOT_SQUARE,
        fitz.PDF_ANNOT_CIRCLE,
        fitz.PDF_ANNOT_POLYGON,
        fitz.PDF_ANNOT_POLY_LINE,
        fitz.PDF_ANNOT_HIGHLIGHT,
        fitz.PDF_ANNOT_UNDERLINE,
        fitz.PDF_ANNOT_SQUIGGLY,
        fitz.PDF_ANNOT_STRIKE_OUT,
        fitz.PDF_ANNOT_CARET,
        fitz.PDF_ANNOT_INK,
    }
    if annotation_type not in supported:
        raise OutputValidationError(f"PDF contains unsupported annotation type {type_name}")
    for key in ("A", "AA", "FS", "JS", "Sound", "Movie", "RichMediaContent", "IRT"):
        if _annotation_action_is_present(document, annotation.xref, key):
            raise OutputValidationError("PDF annotation contains an unsafe or unsupported action")

    rect = _rect_tuple(annotation.rect)
    _ensure_rect_on_page(rect, page_rect)
    vertices = annotation.vertices or []
    if vertices and isinstance(vertices[0], (list, tuple)) and len(vertices[0]) == 2:
        vertex_groups = [[_point_tuple(item) for item in vertices]]
    else:
        vertex_groups = [[_point_tuple(item) for item in group] for group in vertices]
    for group in vertex_groups:
        for point in group:
            _ensure_rect_on_page((point[0], point[1], point[0] + 0.001, point[1] + 0.001), page_rect)

    info = dict(annotation.info or {})
    colors = dict(annotation.colors or {})
    border = dict(annotation.border or {})
    return {
        "type": int(annotation_type),
        "rect": rect,
        "vertices": vertex_groups,
        "info": {
            key: str(info.get(key) or "")
            for key in ("content", "title", "subject", "creationDate", "modDate", "name")
        },
        "colors": {
            "stroke": tuple(colors["stroke"]) if colors.get("stroke") else None,
            "fill": tuple(colors["fill"]) if colors.get("fill") else None,
        },
        "border": border,
        "opacity": float(annotation.opacity),
        "flags": int(annotation.flags),
        "rotation": int(annotation.rotation),
        "line_ends": tuple(annotation.line_ends or (0, 0)),
        "is_open": bool(annotation.is_open),
    }


def _annotation_signature(spec: dict[str, Any]) -> tuple[Any, ...]:
    vertices = tuple(
        tuple(_geometry_signature(point) for point in group)
        for group in spec["vertices"]
    )
    return (
        spec["type"],
        (
            tuple(round(item) for item in spec["rect"])
            if fitz is not None and spec["type"] == fitz.PDF_ANNOT_TEXT
            else _geometry_signature(spec["rect"])
        ),
        spec["info"]["content"],
        vertices,
    )


def _xref_keys(document: Any, xref: int) -> frozenset[str]:
    if not isinstance(xref, int) or xref <= 0:
        raise OutputValidationError("PDF contains an invalid object reference")
    keys = document.xref_get_keys(xref)
    if (
        not isinstance(keys, (list, tuple))
        or not keys
        or any(not isinstance(key, str) or not key for key in keys)
    ):
        raise OutputValidationError("PDF object dictionary could not be validated")
    return frozenset(keys)


def _xref_key(document: Any, xref: int, key: str) -> tuple[str, str]:
    result = document.xref_get_key(xref, key)
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or not all(isinstance(item, str) for item in result)
    ):
        raise OutputValidationError("PDF object value could not be validated")
    return result


def _reject_unsafe_global_entries(document: Any) -> None:
    """Reject active catalog, page and attachment roots before normalization."""
    try:
        catalog_xref = document.pdf_catalog()
        catalog_keys = _xref_keys(document, catalog_xref)
        if catalog_keys & _UNSAFE_CATALOG_KEYS:
            raise OutputValidationError("PDF catalog contains active or attached content")

        if "Names" in catalog_keys:
            names_type, _names_value = _xref_key(document, catalog_xref, "Names")
            if names_type != "null" and names_type not in {"dict", "xref"}:
                raise OutputValidationError("PDF catalog name tree is malformed")
            if names_type != "null":
                for key in _UNSAFE_NAME_TREE_KEYS:
                    value_type, _value = _xref_key(
                        document,
                        catalog_xref,
                        f"Names/{key}",
                    )
                    if value_type != "null":
                        raise OutputValidationError(
                            "PDF catalog name tree contains active or attached content"
                        )

        embedded_files = document.embfile_count()
        if not isinstance(embedded_files, int) or embedded_files < 0:
            raise OutputValidationError("PDF embedded files could not be validated")
        if embedded_files:
            raise OutputValidationError("PDF contains embedded files")

        page_count = document.page_count
        if not isinstance(page_count, int) or page_count < 1:
            raise OutputValidationError("PDF page tree could not be validated")
        for page_index in range(page_count):
            page_xref = document.page_xref(page_index)
            if _xref_keys(document, page_xref) & _UNSAFE_PAGE_KEYS:
                raise OutputValidationError("PDF page contains active or attached content")
    except OutputValidationError:
        raise
    except Exception as error:
        raise OutputValidationError("PDF global objects could not be validated") from error


def _scan_interactivity(document: Any) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    _reject_unsafe_global_entries(document)
    page_rects = [document[index].rect for index in range(document.page_count)]
    named_destinations = document.resolve_names()
    links_by_page: list[list[dict[str, Any]]] = []
    annotations_by_page: list[list[dict[str, Any]]] = []
    for page_index in range(document.page_count):
        page = document[page_index]
        if list(page.widgets() or []):
            raise OutputValidationError("PDF contains unsupported interactive form fields")
        links_by_page.append(
            [
                _safe_link_spec(
                    link,
                    page_count=document.page_count,
                    page_rect=page.rect,
                    target_page_rects=page_rects,
                    named_destinations=named_destinations,
                )
                for link in page.get_links()
            ]
        )
        annotations_by_page.append(
            [_safe_annotation_spec(document, page.rect, annotation) for annotation in page.annots()]
        )
    return links_by_page, annotations_by_page


def _safe_source_toc(document: Any) -> list[list[Any]]:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    source_toc = document.get_toc(simple=False)
    safe_toc: list[list[Any]] = []
    previous_level = 0
    for item in source_toc:
        if len(item) < 4 or not isinstance(item[3], dict):
            raise OutputValidationError("PDF outline has an unsupported destination")
        level, title, page_number, destination = item[:4]
        if (
            not isinstance(level, int)
            or level < 1
            or level > previous_level + 1
            or not isinstance(page_number, int)
            or not 1 <= page_number <= document.page_count
        ):
            raise OutputValidationError("PDF outline has an invalid level or page target")
        if (
            int(destination.get("kind", fitz.LINK_NONE)) != fitz.LINK_GOTO
            or destination.get("file")
            or destination.get("uri")
            or int(destination.get("page", page_number - 1)) != page_number - 1
        ):
            raise OutputValidationError("PDF outline contains an external or unsupported action")
        previous_level = level
        safe_toc.append([level, str(title), page_number])
    return safe_toc


def _image_signatures(document: Any) -> list[Counter[tuple[Any, ...]]]:
    signatures: list[Counter[tuple[Any, ...]]] = []
    for page_index in range(document.page_count):
        page_signatures: Counter[tuple[Any, ...]] = Counter()
        for image in document[page_index].get_image_info(hashes=True):
            digest = image.get("digest")
            if not isinstance(digest, bytes):
                raise OutputValidationError("PDF image digest could not be verified")
            bbox = _rect_tuple(image.get("bbox"))
            page_signatures[
                (
                    _geometry_signature(bbox),
                    digest,
                    int(image.get("width") or 0),
                    int(image.get("height") or 0),
                )
            ] += 1
        signatures.append(page_signatures)
    return signatures


def _rendered_clip_digest(page: Any, bbox: tuple[float, float, float, float]) -> bytes:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    pixmap = page.get_pixmap(clip=fitz.Rect(bbox), alpha=False, annots=False)
    return hashlib.sha256(pixmap.samples).digest()


def _restore_visible_source_figures(source_page: Any, output_page: Any) -> None:
    """Repaint source figures that upstream retained but later covered.

    BabelDOC can preserve an image XObject and its placement while drawing an
    opaque translated text layer over it. Resource-level checks therefore pass
    even though the figure is invisible. Repaint only non-page-sized figures
    whose rendered pixels changed; full-page scan images are intentionally
    excluded so an OCR translation layer is not hidden again.
    """
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    page_area = float(source_page.rect.width) * float(source_page.rect.height)
    if page_area <= 0:
        raise OutputValidationError("PDF page geometry is invalid")
    for image in source_page.get_image_info(hashes=True):
        bbox = _rect_tuple(image.get("bbox"))
        rect = fitz.Rect(bbox) & source_page.rect
        if rect.is_empty or rect.is_infinite:
            raise OutputValidationError("PDF image placement is invalid")
        if float(rect.width) * float(rect.height) / page_area >= 0.8:
            continue
        if _rendered_clip_digest(source_page, bbox) == _rendered_clip_digest(output_page, bbox):
            continue
        pixmap = source_page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            clip=rect,
            alpha=False,
            annots=False,
        )
        output_page.insert_image(rect, stream=pixmap.tobytes("png"), overlay=True)


def _add_annotation(page: Any, spec: dict[str, Any]) -> Any:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    annotation_type = spec["type"]
    rect = fitz.Rect(spec["rect"])
    groups = [[fitz.Point(point) for point in group] for group in spec["vertices"]]
    content = spec["info"]["content"]
    if annotation_type == fitz.PDF_ANNOT_TEXT:
        annotation = page.add_text_annot(rect.tl, content, icon=spec["info"]["name"] or "Note")
    elif annotation_type == fitz.PDF_ANNOT_FREE_TEXT:
        annotation = page.add_freetext_annot(
            rect,
            content,
            border_color=spec["colors"]["stroke"],
            text_color=spec["colors"]["stroke"],
            fill_color=spec["colors"]["fill"],
            rotate=spec["rotation"],
        )
    elif annotation_type in {
        fitz.PDF_ANNOT_HIGHLIGHT,
        fitz.PDF_ANNOT_UNDERLINE,
        fitz.PDF_ANNOT_SQUIGGLY,
        fitz.PDF_ANNOT_STRIKE_OUT,
    }:
        if not groups or len(groups[0]) % 4:
            raise OutputValidationError("Text markup annotation has invalid quadrilaterals")
        quads = [fitz.Quad(groups[0][index : index + 4]) for index in range(0, len(groups[0]), 4)]
        method = {
            fitz.PDF_ANNOT_HIGHLIGHT: page.add_highlight_annot,
            fitz.PDF_ANNOT_UNDERLINE: page.add_underline_annot,
            fitz.PDF_ANNOT_SQUIGGLY: page.add_squiggly_annot,
            fitz.PDF_ANNOT_STRIKE_OUT: page.add_strikeout_annot,
        }[annotation_type]
        annotation = method(quads)
    elif annotation_type == fitz.PDF_ANNOT_CARET:
        annotation = page.add_caret_annot(rect.tl)
    elif annotation_type == fitz.PDF_ANNOT_INK:
        annotation = page.add_ink_annot(groups)
    elif annotation_type == fitz.PDF_ANNOT_LINE:
        if not groups or len(groups[0]) != 2:
            raise OutputValidationError("Line annotation has invalid vertices")
        annotation = page.add_line_annot(*groups[0])
    elif annotation_type == fitz.PDF_ANNOT_SQUARE:
        annotation = page.add_rect_annot(rect)
    elif annotation_type == fitz.PDF_ANNOT_CIRCLE:
        annotation = page.add_circle_annot(rect)
    elif annotation_type == fitz.PDF_ANNOT_POLYGON:
        annotation = page.add_polygon_annot(groups[0])
    elif annotation_type == fitz.PDF_ANNOT_POLY_LINE:
        annotation = page.add_polyline_annot(groups[0])
    else:  # The reader rejects unsupported types before reaching this branch.
        raise OutputValidationError("PDF contains an unsupported annotation")

    annotation.set_info(
        content=content,
        title=spec["info"]["title"],
        subject=spec["info"]["subject"],
        creationDate=spec["info"]["creationDate"],
        modDate=spec["info"]["modDate"],
    )
    if annotation_type == fitz.PDF_ANNOT_TEXT:
        annotation.set_rect(rect)
    annotation.set_colors(**spec["colors"])
    if spec["border"]:
        annotation.set_border(spec["border"])
    annotation.set_opacity(spec["opacity"])
    annotation.set_flags(spec["flags"])
    if annotation_type != fitz.PDF_ANNOT_FREE_TEXT and spec["rotation"]:
        annotation.set_rotation(spec["rotation"])
    if annotation_type in {
        fitz.PDF_ANNOT_LINE,
        fitz.PDF_ANNOT_POLYGON,
        fitz.PDF_ANNOT_POLY_LINE,
    }:
        annotation.set_line_ends(spec["line_ends"])
    if annotation_type == fitz.PDF_ANNOT_TEXT:
        annotation.set_open(spec["is_open"])
    annotation.update()
    return annotation


def _insert_safe_link(page: Any, spec: dict[str, Any]) -> None:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    link = dict(spec)
    link["from"] = fitz.Rect(link["from"])
    if link["kind"] == fitz.LINK_NAMED:
        link = {
            "kind": fitz.LINK_GOTO,
            "from": link["from"],
            "page": -1,
            "to": link["nameddest"],
        }
    elif "to" in link:
        link["to"] = fitz.Point(link["to"])
    page.insert_link(link)


def _restore_safe_interactivity(input_path: Path, output_path: Path) -> None:
    if fitz is None:
        raise OutputValidationError("PyMuPDF is unavailable")
    temporary_path = output_path.with_name(f".{output_path.name}.interactive.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        with fitz.open(input_path) as source_document, fitz.open(output_path) as output_document:
            if source_document.page_count != output_document.page_count:
                raise OutputValidationError("Translated PDF page count does not match the source")
            for page_index in range(source_document.page_count):
                source_rect = source_document[page_index].rect
                output_rect = output_document[page_index].rect
                if (
                    abs(float(source_rect.width) - float(output_rect.width)) > 0.5
                    or abs(float(source_rect.height) - float(output_rect.height)) > 0.5
                ):
                    raise OutputValidationError("Translated PDF page geometry does not match the source")

            expected_links, expected_annotations = _scan_interactivity(source_document)
            expected_toc = _safe_source_toc(source_document)
            current_links, current_annotations = _scan_interactivity(output_document)
            for page_index in range(source_document.page_count):
                page = output_document[page_index]
                _restore_visible_source_figures(source_document[page_index], page)
                current_link_counts = Counter(_link_signature(item) for item in current_links[page_index])
                for spec in expected_links[page_index]:
                    signature = _link_signature(spec)
                    if current_link_counts[signature] > 0:
                        current_link_counts[signature] -= 1
                        continue
                    _insert_safe_link(page, spec)

                current_annotation_counts = Counter(
                    _annotation_signature(item) for item in current_annotations[page_index]
                )
                for spec in expected_annotations[page_index]:
                    signature = _annotation_signature(spec)
                    if current_annotation_counts[signature] > 0:
                        current_annotation_counts[signature] -= 1
                        continue
                    _add_annotation(page, spec)

            output_document.set_toc(expected_toc)
            output_document.save(temporary_path, garbage=4, deflate=True, clean=True)
        if not temporary_path.is_file():
            raise OutputValidationError("Translated PDF normalization did not produce an output")
        os.replace(temporary_path, output_path)

        with fitz.open(input_path) as source_document, fitz.open(output_path) as output_document:
            if source_document.page_count != output_document.page_count:
                raise OutputValidationError("Translated PDF page count changed during normalization")
            for page_index in range(source_document.page_count):
                source_rect = source_document[page_index].rect
                output_rect = output_document[page_index].rect
                if (
                    abs(float(source_rect.width) - float(output_rect.width)) > 0.5
                    or abs(float(source_rect.height) - float(output_rect.height)) > 0.5
                ):
                    raise OutputValidationError("Translated PDF page geometry changed during normalization")
            expected_links, expected_annotations = _scan_interactivity(source_document)
            actual_links, actual_annotations = _scan_interactivity(output_document)
            expected_toc = _safe_source_toc(source_document)
            if output_document.get_toc() != expected_toc:
                raise OutputValidationError("Translated PDF did not preserve its source outline")
            expected_images = _image_signatures(source_document)
            actual_images = _image_signatures(output_document)
            for page_index in range(source_document.page_count):
                if not expected_images[page_index] <= actual_images[page_index]:
                    raise OutputValidationError("Translated PDF did not preserve every source image")
                if not Counter(_link_signature(item) for item in expected_links[page_index]) <= Counter(
                    _link_signature(item) for item in actual_links[page_index]
                ):
                    raise OutputValidationError("Translated PDF did not preserve every safe link")
                if not Counter(
                    _annotation_signature(item) for item in expected_annotations[page_index]
                ) <= Counter(_annotation_signature(item) for item in actual_annotations[page_index]):
                    raise OutputValidationError("Translated PDF did not preserve every safe annotation")
    except OutputValidationError:
        raise
    except Exception as error:
        raise OutputValidationError("Translated PDF interactivity could not be validated") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _make_settings(output_dir: Path):
    if any(
        item is None
        for item in (
            BasicSettings,
            PDFSettings,
            SettingsModel,
            TranslationSettings,
            OpenAISettings,
            do_translate_async_stream,
        )
    ):
        raise RuntimeError("PDFMathTranslate-next runtime is unavailable")
    base_url = os.environ.get(
        "PEINIDU_PDF_EXPORT_INTERNAL_BASE_URL",
        "http://backend:8000/internal/llm/v1",
    ).rstrip("/")
    if not base_url.endswith("/v1"):
        raise RuntimeError("Internal LLM base URL must end with /v1")
    token = _token()
    if not token:
        raise RuntimeError("PDF export internal token is not configured")
    return SettingsModel(
        basic=BasicSettings(debug=False, gui=False),
        translation=TranslationSettings(
            lang_in="en",
            lang_out="zh-CN",
            output=str(output_dir),
            qps=1,
            pool_max_workers=1,
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            no_dual=True,
            no_mono=False,
            watermark_output_mode="watermarked",
        ),
        translate_engine_settings=OpenAISettings(
            openai_model=MODEL_ALIAS,
            openai_base_url=base_url,
            openai_api_key=token,
            openai_timeout=str(min(MAX_JOB_SECONDS, 300)),
            openai_enable_json_mode=False,
            openai_send_temprature=False,
            openai_send_reasoning_effort=False,
        ),
        report_interval=0.5,
    )


def _translation_events(settings: Any, input_path: Path) -> AsyncIterator[dict[str, Any]]:
    if do_translate_async_stream is None:
        raise RuntimeError("PDFMathTranslate-next runtime is unavailable")
    return do_translate_async_stream(settings, input_path)


def _retain_mono_pdf(job: ExportJob, mono_path: str | os.PathLike[str] | None) -> None:
    if not mono_path:
        raise RuntimeError("Translation completed without a monolingual PDF")
    source = Path(mono_path).resolve()
    output_root = (job.directory / "output").resolve()
    if not source.is_file() or output_root not in source.parents:
        raise RuntimeError("Translation returned an invalid result path")
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = job.output_path.with_name(f".{job.output_path.name}.pending")
    staging_path.unlink(missing_ok=True)
    try:
        shutil.copy2(source, staging_path)
        _restore_safe_interactivity(job.input_path, staging_path)
        if _validate_pdf(staging_path) != job.page_count:
            raise OutputValidationError("Translated PDF page count changed during publication")
        os.replace(staging_path, job.output_path)
    finally:
        staging_path.unlink(missing_ok=True)
    for child in list(output_root.iterdir()):
        if child.resolve() == job.output_path.resolve():
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


async def _translate_job(job: ExportJob) -> None:
    job.status = "running"
    job.stage = "initializing"
    job.updated_at = time.time()
    finished = False
    try:
        settings = _make_settings(job.directory / "output")
        async with asyncio.timeout(MAX_JOB_SECONDS):
            async for event in _translation_events(settings, job.input_path):
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or "")
                progress, stage = _event_progress(event)
                if progress is not None:
                    job.progress = progress
                if stage:
                    job.stage = stage
                job.updated_at = time.time()
                if event_type == "error":
                    code, message = _safe_error(event.get("error") or "translation error")
                    raise RuntimeError(f"{code}: {message}")
                if event_type == "finish":
                    result = event.get("translate_result")
                    mono_path = getattr(result, "mono_pdf_path", None)
                    if mono_path is None and isinstance(result, dict):
                        mono_path = result.get("mono_pdf_path")
                    await asyncio.to_thread(_retain_mono_pdf, job, mono_path)
                    finished = True
                    break
        if not finished:
            raise RuntimeError("Translation ended without a finish event")
        job.status = "done"
        job.stage = "done"
        job.progress = 1.0
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.stage = "cancelled"
        job.error_code = "cancelled"
        job.error_message = "The PDF export was cancelled."
        shutil.rmtree(job.directory, ignore_errors=True)
        raise
    except TimeoutError:
        job.status = "error"
        job.stage = "error"
        job.error_code = "provider_timeout"
        job.error_message = "The PDF export exceeded its time limit."
        shutil.rmtree(job.directory / "output", ignore_errors=True)
    except Exception as error:
        job.status = "error"
        job.stage = "error"
        job.error_code, job.error_message = _safe_error(error)
        shutil.rmtree(job.directory / "output", ignore_errors=True)
    finally:
        job.updated_at = time.time()


@app.get("/health", dependencies=[Depends(_require_bearer)])
async def health() -> dict[str, Any]:
    cache = Path.home() / ".cache"
    runtime_ready = _UPSTREAM_IMPORT_ERROR is None and (cache / ".pet-preloaded").is_file()
    if not runtime_ready:
        raise HTTPException(status_code=503, detail="PDF export runtime is unavailable")
    return {"status": "ok", "upstream_version": UPSTREAM_VERSION}


@app.get("/info", dependencies=[Depends(_require_bearer)])
async def info() -> dict[str, Any]:
    try:
        wrapper_source_sha256 = _wrapper_source_sha256()
    except OSError as error:
        raise HTTPException(
            status_code=503,
            detail="PDF export wrapper source is unavailable",
        ) from error
    return {
        "name": "PDFMathTranslate-next",
        "version": UPSTREAM_VERSION,
        "revision": UPSTREAM_REVISION,
        "image": UPSTREAM_IMAGE,
        "source": UPSTREAM_SOURCE,
        "license": UPSTREAM_LICENSE,
        "wrapper_version": WRAPPER_VERSION,
        "wrapper_source_sha256": wrapper_source_sha256,
        "output": "monolingual-watermarked-zh-CN",
    }


@app.post("/jobs", status_code=202, dependencies=[Depends(_require_bearer)])
async def create_job(
    job_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    directory = _job_directory(job_id)
    async with _jobs_lock:
        if job_id in _jobs:
            raise HTTPException(status_code=409, detail="PDF export job already exists")
        active = sum(job.status not in _TERMINAL_STATES for job in _jobs.values())
        if active >= MAX_CONCURRENT_JOBS:
            raise HTTPException(status_code=429, detail="PDF export concurrency limit reached")
        directory.mkdir(parents=True, exist_ok=False)
        input_path = directory / "input.pdf"
        try:
            await _save_upload(file, input_path)
            page_count = await asyncio.to_thread(_validate_pdf, input_path)
        except OutputValidationError as error:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(
                status_code=422,
                detail="Uploaded PDF contains unsupported active content",
            ) from error
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        job = ExportJob(
            job_id=job_id,
            directory=directory,
            input_path=input_path,
            output_path=directory / "output" / "translated.zh-CN.pdf",
            page_count=page_count,
        )
        _jobs[job_id] = job
        job.task = asyncio.create_task(_translate_job(job), name=f"pdf-export-{job_id}")
    return job.public()


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_bearer)])
async def get_job(job_id: str) -> dict[str, Any]:
    return _require_job(job_id).public()


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(_require_bearer)])
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = _require_job(job_id)
    if job.status in _TERMINAL_STATES:
        if job.status == "cancelled":
            return job.public()
        raise HTTPException(status_code=409, detail="PDF export job is already terminal")
    task = job.task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if job.status not in _TERMINAL_STATES:
        job.status = "cancelled"
        job.stage = "cancelled"
        job.error_code = "cancelled"
        job.error_message = "The PDF export was cancelled."
        job.updated_at = time.time()
        shutil.rmtree(job.directory, ignore_errors=True)
    return job.public()


@app.get("/jobs/{job_id}/download", dependencies=[Depends(_require_bearer)])
async def download_job(job_id: str):
    job = _require_job(job_id)
    if job.status != "done" or not job.output_path.is_file():
        raise HTTPException(status_code=409, detail="PDF export is not ready")
    return FileResponse(
        job.output_path,
        media_type="application/pdf",
        filename=f"{job.job_id}.zh-CN.pdf",
    )


@app.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(_require_bearer)])
async def delete_job(job_id: str) -> None:
    job = _require_job(job_id)
    if job.status not in _TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="Cancel the PDF export before cleanup")
    shutil.rmtree(job.directory, ignore_errors=True)
    async with _jobs_lock:
        _jobs.pop(job_id, None)
