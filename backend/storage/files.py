"""文件系统存储 — 大文本持久化（D11）。

每篇论文一个目录：data/papers/{arxiv_id}/
  - original.md        原文（Markdown 形式）
  - translation.json   blocks + 逐段译文（断点续翻缓存，D16）
  - analysis.json      Agent 分析结果
  - annotations.json   用户划线 / 高亮 / 批注
  - translation_layout.json  PDF 原位译文版面契约（不含译文）
  - mineru_layout_generations/{id}/  同一代 MinerU 版面与 content list
  - mineru_layout_meta.json  当前 MinerU generation 的原子 manifest
  - mineru_source_meta.json  独立于 layout cache 的 OCR 来源信息
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..extraction.blocks import Block, PaperDocument


ANNOTATION_KINDS = {
    "highlight",
    "important",
    "question",
    "method",
    "conclusion",
}
ANNOTATION_KIND_COLORS = {
    "highlight": "yellow",
    "important": "amber",
    "question": "rose",
    "method": "blue",
    "conclusion": "green",
}
PAPER_NOTE_FILENAME = "paper_note.md"


class PaperNoteRevisionConflict(ValueError):
    def __init__(self, current_revision: str) -> None:
        super().__init__("paper note revision conflict")
        self.current_revision = current_revision

# data/ 目录（backend/storage/files.py → 上两级 → data/）。
# 本地 Core 安装目录可只读，因此允许启动器把运行缓存定向到用户目录。
DATA_DIR = Path(
    os.environ.get(
        "PEINIDU_DATA_DIR",
        str(Path(__file__).resolve().parents[2] / "data"),
    )
).expanduser()
PAPERS_DIR = DATA_DIR / "papers"
COLLECTIONS_DIR = DATA_DIR / "collections"

_MINERU_ARTIFACT_SCHEMA_VERSION = 2
_MINERU_SOURCE_META_SCHEMA_VERSION = 1
_MINERU_ARTIFACT_ADAPTER = "mineru_middle"
_MINERU_ARTIFACT_ADAPTER_VERSION = "1"
_MINERU_GENERATIONS_DIR = "mineru_layout_generations"
_MINERU_GENERATIONS_TO_KEEP = 3


def _atomic_write_text(path: Path, text: str) -> None:
    """写临时文件后原子替换，避免崩溃留下半截 JSON/Markdown。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_json(path: Path, data) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def paper_dir(arxiv_id: str) -> Path:
    """论文目录路径。"""
    return PAPERS_DIR / arxiv_id


def ensure_paper_dir(arxiv_id: str) -> Path:
    d = paper_dir(arxiv_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def collection_dir(collection_id: int) -> Path:
    return COLLECTIONS_DIR / str(collection_id)


def ensure_collection_dir(collection_id: int) -> Path:
    d = collection_dir(collection_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_document(doc: PaperDocument) -> None:
    """保存 translation.json（含 blocks）。同时写一份 original.md。"""
    d = ensure_paper_dir(doc.paper_id)
    # translation.json
    _write_json(d / "translation.json", doc.to_dict())
    # original.md（原文 Markdown 形式，便于人读 / git 追踪）
    md_parts: list[str] = []
    for b in doc.blocks:
        if b.type == "heading":
            md_parts.append(f"{'#' * (b.level or 1)} {b.original}")
        elif b.type == "formula":
            md_parts.append(f"$$ {b.original} $$")
        else:
            md_parts.append(b.original)
    _atomic_write_text(d / "original.md", "\n\n".join(md_parts))


def load_document(arxiv_id: str) -> PaperDocument | None:
    """读取 translation.json，返回 PaperDocument。不存在返回 None。"""
    path = paper_dir(arxiv_id) / "translation.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return PaperDocument.from_dict(data)


def update_block_translation(arxiv_id: str, index: int, translation: str, status: str) -> None:
    """更新单个 block 的译文 + status，并持久化（断点续翻，D16）。"""
    doc = load_document(arxiv_id)
    if doc is None:
        return
    for b in doc.blocks:
        if b.index == index:
            b.translation = translation
            b.status = status  # type: ignore[assignment]
            break
    save_document(doc)


def update_block_status(arxiv_id: str, index: int, status: str) -> None:
    """只更新 block status，用于记录翻译失败等无译文状态。"""
    doc = load_document(arxiv_id)
    if doc is None:
        return
    for b in doc.blocks:
        if b.index == index:
            b.status = status  # type: ignore[assignment]
            break
    save_document(doc)


def save_analysis(arxiv_id: str, analysis: dict) -> None:
    """保存 Agent 分析结果到 analysis.json。"""
    d = ensure_paper_dir(arxiv_id)
    _write_json(d / "analysis.json", analysis)


def load_analysis(arxiv_id: str) -> dict | None:
    path = paper_dir(arxiv_id) / "analysis.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_extraction_quality(arxiv_id: str, report: dict) -> None:
    """保存提取质量报告，便于回归和用户可信度展示。"""
    d = ensure_paper_dir(arxiv_id)
    _write_json(d / "extraction_quality.json", report)


def load_extraction_quality(arxiv_id: str) -> dict | None:
    path = paper_dir(arxiv_id) / "extraction_quality.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_block_pdf_map(arxiv_id: str, mapping: dict) -> None:
    d = ensure_paper_dir(arxiv_id)
    _write_json(d / "block_to_pdf_map.json", mapping)


def load_block_pdf_map(arxiv_id: str) -> dict | None:
    path = paper_dir(arxiv_id) / "block_to_pdf_map.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_translation_layout(arxiv_id: str, layout: dict) -> None:
    d = ensure_paper_dir(arxiv_id)
    _write_json(d / "translation_layout.json", layout)


def load_translation_layout(arxiv_id: str) -> dict | None:
    path = paper_dir(arxiv_id) / "translation_layout.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_mineru_layout_artifacts(
    arxiv_id: str,
    layout: dict,
    content_list: list[dict],
    *,
    source_pdf_sha256: str | None = None,
    is_ocr: bool | None = None,
) -> str:
    """Atomically publish one immutable MinerU artifact generation."""
    if is_ocr is not None and not isinstance(is_ocr, bool):
        raise TypeError("is_ocr must be a bool or None")

    d = ensure_paper_dir(arxiv_id)
    generation = uuid4().hex
    generations_dir = d / _MINERU_GENERATIONS_DIR
    generations_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = generations_dir / f".{generation}.tmp"
    generation_dir = generations_dir / generation
    staging_dir.mkdir()
    manifest = {
        "schema_version": _MINERU_ARTIFACT_SCHEMA_VERSION,
        "adapter": _MINERU_ARTIFACT_ADAPTER,
        "adapter_version": _MINERU_ARTIFACT_ADAPTER_VERSION,
        "source_pdf_sha256": source_pdf_sha256,
        "is_ocr": is_ocr,
        "generation": generation,
    }
    published = False
    try:
        _write_json(staging_dir / "mineru_middle.json", layout)
        _write_json(staging_dir / "mineru_content_list.json", content_list)
        _write_json(staging_dir / "meta.json", manifest)
        os.replace(staging_dir, generation_dir)

        # This atomic pointer switch is the only publication step. Until it
        # succeeds, readers continue to use the previous complete generation.
        _write_json(
            d / "mineru_layout_meta.json",
            manifest,
        )
        published = True

        # OCR provenance is deliberately outside the layout cache. Publish it
        # only after the generation pointer has switched so an interrupted
        # write can never describe an unpublished generation as current.
        if source_pdf_sha256 is not None:
            _write_json(
                d / "mineru_source_meta.json",
                {
                    "schema_version": _MINERU_SOURCE_META_SCHEMA_VERSION,
                    "adapter": _MINERU_ARTIFACT_ADAPTER,
                    "adapter_version": _MINERU_ARTIFACT_ADAPTER_VERSION,
                    "source_pdf_sha256": source_pdf_sha256,
                    "is_ocr": is_ocr,
                    "generation": generation,
                },
            )
        _prune_mineru_generations(generations_dir, current=generation)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if not published:
            shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    return generation


def _prune_mineru_generations(generations_dir: Path, *, current: str) -> None:
    """Bound immutable generation storage while retaining rollback headroom."""
    candidates: list[Path] = []
    try:
        entries = list(generations_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            is_directory = entry.is_dir()
        except OSError:
            continue
        if is_directory and _is_mineru_generation_id(entry.name):
            candidates.append(entry)
    def modified_at(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    keep = {current}
    for entry in sorted(
        (item for item in candidates if item.name != current),
        key=modified_at,
        reverse=True,
    )[: _MINERU_GENERATIONS_TO_KEEP - 1]:
        keep.add(entry.name)
    for entry in candidates:
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def load_mineru_layout_artifacts(
    arxiv_id: str,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> tuple[dict, list[dict]] | None:
    """Load MinerU payloads with the historical two-item return shape."""
    bundle = load_mineru_layout_artifact_bundle(
        arxiv_id,
        expected_source_pdf_sha256=expected_source_pdf_sha256,
    )
    if bundle is None:
        return None
    return bundle[0], bundle[1]


def load_mineru_layout_artifact_bundle(
    arxiv_id: str,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> tuple[dict, list[dict], dict] | None:
    """Load one complete artifact generation plus normalized provenance."""
    return load_mineru_layout_artifact_bundle_from_dir(
        paper_dir(arxiv_id),
        expected_source_pdf_sha256=expected_source_pdf_sha256,
    )


def load_mineru_layout_artifact_bundle_from_dir(
    directory: Path,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> tuple[dict, list[dict], dict] | None:
    """Load one validated MinerU generation from an explicit paper directory.

    This path-based form lets read-only audits inspect an arbitrary
    ``--papers-dir`` without rebinding the process-wide storage root. The
    generation identifier is validated before it is used as a child path.
    """
    d = Path(directory)
    if not d.is_dir():
        return None
    manifest = _read_json_object(d / "mineru_layout_meta.json")
    if manifest is not None and (
        manifest.get("schema_version") == _MINERU_ARTIFACT_SCHEMA_VERSION
        or "generation" in manifest
    ):
        normalized_meta = _normalize_mineru_generation_meta(
            manifest,
            expected_source_pdf_sha256=expected_source_pdf_sha256,
        )
        if normalized_meta is None:
            return None
        generation = normalized_meta["generation"]
        generation_dir = d / _MINERU_GENERATIONS_DIR / generation
        generation_meta = _read_json_object(generation_dir / "meta.json")
        if generation_meta is None:
            return None
        normalized_generation_meta = _normalize_mineru_generation_meta(
            generation_meta,
            expected_source_pdf_sha256=expected_source_pdf_sha256,
        )
        if normalized_generation_meta != normalized_meta:
            return None
        payload = _load_mineru_artifact_pair(
            generation_dir / "mineru_middle.json",
            generation_dir / "mineru_content_list.json",
        )
        if payload is None:
            return None
        return payload[0], payload[1], normalized_meta

    # Legacy flat files remain readable. Missing OCR/generation fields are
    # explicitly reported as unknown instead of being guessed.
    layout_path = d / "mineru_middle.json"
    content_list_path = d / "mineru_content_list.json"
    payload = _load_mineru_artifact_pair(layout_path, content_list_path)
    if payload is None:
        return None
    if expected_source_pdf_sha256 is not None:
        if (
            manifest is None
            or manifest.get("adapter") != _MINERU_ARTIFACT_ADAPTER
            or manifest.get("adapter_version") != _MINERU_ARTIFACT_ADAPTER_VERSION
            or manifest.get("source_pdf_sha256") != expected_source_pdf_sha256
        ):
            return None
    source_hash = manifest.get("source_pdf_sha256") if manifest is not None else None
    is_ocr = manifest.get("is_ocr") if manifest is not None else None
    if not isinstance(is_ocr, bool):
        source_meta = load_mineru_source_meta_from_dir(
            d,
            expected_source_pdf_sha256=(
                expected_source_pdf_sha256
                if expected_source_pdf_sha256 is not None
                else source_hash if isinstance(source_hash, str) else None
            ),
        )
        is_ocr = source_meta["is_ocr"] if source_meta is not None else None
    return (
        payload[0],
        payload[1],
        {
            "schema_version": 1,
            "adapter": _MINERU_ARTIFACT_ADAPTER,
            "adapter_version": _MINERU_ARTIFACT_ADAPTER_VERSION,
            "source_pdf_sha256": source_hash if isinstance(source_hash, str) else None,
            "is_ocr": is_ocr,
            "generation": None,
        },
    )


def load_mineru_layout_provenance(
    arxiv_id: str,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> dict | None:
    """Read only the current MinerU generation manifests, not large payloads."""
    d = paper_dir(arxiv_id)
    manifest = _read_json_object(d / "mineru_layout_meta.json")
    if manifest is None:
        return None
    normalized = _normalize_mineru_generation_meta(
        manifest,
        expected_source_pdf_sha256=expected_source_pdf_sha256,
    )
    if normalized is None:
        return None
    generation_meta = _read_json_object(
        d / _MINERU_GENERATIONS_DIR / normalized["generation"] / "meta.json"
    )
    if generation_meta is None:
        return None
    normalized_generation = _normalize_mineru_generation_meta(
        generation_meta,
        expected_source_pdf_sha256=expected_source_pdf_sha256,
    )
    return normalized if normalized_generation == normalized else None


def load_mineru_source_meta(
    arxiv_id: str,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> dict | None:
    """Load OCR provenance even when layout artifacts were invalidated."""
    return load_mineru_source_meta_from_dir(
        paper_dir(arxiv_id),
        expected_source_pdf_sha256=expected_source_pdf_sha256,
    )


def load_mineru_source_meta_from_dir(
    directory: Path,
    *,
    expected_source_pdf_sha256: str | None = None,
) -> dict | None:
    """Load OCR provenance from an explicit, caller-selected paper directory."""
    meta = _read_json_object(Path(directory) / "mineru_source_meta.json")
    if meta is None:
        return None
    if (
        meta.get("schema_version") != _MINERU_SOURCE_META_SCHEMA_VERSION
        or meta.get("adapter") != _MINERU_ARTIFACT_ADAPTER
        or meta.get("adapter_version") != _MINERU_ARTIFACT_ADAPTER_VERSION
    ):
        return None
    source_hash = meta.get("source_pdf_sha256")
    if not isinstance(source_hash, str):
        return None
    if expected_source_pdf_sha256 is not None and source_hash != expected_source_pdf_sha256:
        return None
    is_ocr = meta.get("is_ocr")
    if is_ocr is not None and not isinstance(is_ocr, bool):
        return None
    generation = meta.get("generation")
    if generation is not None and not _is_mineru_generation_id(generation):
        return None
    return {
        "schema_version": _MINERU_SOURCE_META_SCHEMA_VERSION,
        "adapter": _MINERU_ARTIFACT_ADAPTER,
        "adapter_version": _MINERU_ARTIFACT_ADAPTER_VERSION,
        "source_pdf_sha256": source_hash,
        "is_ocr": is_ocr,
        "generation": generation,
    }


def _normalize_mineru_generation_meta(
    meta: dict,
    *,
    expected_source_pdf_sha256: str | None,
) -> dict | None:
    if (
        meta.get("schema_version") != _MINERU_ARTIFACT_SCHEMA_VERSION
        or meta.get("adapter") != _MINERU_ARTIFACT_ADAPTER
        or meta.get("adapter_version") != _MINERU_ARTIFACT_ADAPTER_VERSION
    ):
        return None
    source_hash = meta.get("source_pdf_sha256")
    if source_hash is not None and not isinstance(source_hash, str):
        return None
    if expected_source_pdf_sha256 is not None and source_hash != expected_source_pdf_sha256:
        return None
    is_ocr = meta.get("is_ocr")
    if is_ocr is not None and not isinstance(is_ocr, bool):
        return None
    generation = meta.get("generation")
    if not _is_mineru_generation_id(generation):
        return None
    return {
        "schema_version": _MINERU_ARTIFACT_SCHEMA_VERSION,
        "adapter": _MINERU_ARTIFACT_ADAPTER,
        "adapter_version": _MINERU_ARTIFACT_ADAPTER_VERSION,
        "source_pdf_sha256": source_hash,
        "is_ocr": is_ocr,
        "generation": generation,
    }


def _is_mineru_generation_id(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_mineru_artifact_pair(
    layout_path: Path,
    content_list_path: Path,
) -> tuple[dict, list[dict]] | None:
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(layout, dict) or not isinstance(content_list, list):
        return None
    if any(not isinstance(item, dict) for item in content_list):
        return None
    return layout, content_list


def _normalize_annotation(item: dict) -> dict:
    normalized = dict(item)
    kind = normalized.get("kind")
    if kind not in ANNOTATION_KINDS:
        kind = "highlight"
    normalized["kind"] = kind
    normalized.setdefault("color", ANNOTATION_KIND_COLORS[kind])
    normalized.setdefault("note", "")
    normalized.setdefault("updated_at", normalized.get("created_at", ""))
    return normalized


def load_annotations(arxiv_id: str) -> list[dict]:
    path = paper_dir(arxiv_id) / "annotations.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [_normalize_annotation(item) for item in data if isinstance(item, dict)]


def save_annotations(arxiv_id: str, annotations: list[dict]) -> None:
    d = ensure_paper_dir(arxiv_id)
    _write_json(d / "annotations.json", annotations)


def add_annotation(
    arxiv_id: str,
    block_index: int,
    side: str,
    text: str,
    note: str = "",
    color: str = "yellow",
    kind: str = "highlight",
    selector: dict | None = None,
) -> dict:
    if kind not in ANNOTATION_KINDS:
        raise ValueError(f"unsupported annotation kind: {kind}")
    timestamp = now_iso()
    annotation = {
        "id": uuid4().hex,
        "arxiv_id": arxiv_id,
        "block_index": block_index,
        "side": side,
        "text": text,
        "note": note,
        "color": color if kind == "highlight" else ANNOTATION_KIND_COLORS[kind],
        "kind": kind,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if selector is not None:
        annotation["selector"] = selector
    annotations = load_annotations(arxiv_id)
    annotations.append(annotation)
    save_annotations(arxiv_id, annotations)
    return annotation


def update_annotation(
    arxiv_id: str,
    annotation_id: str,
    *,
    note: str | None = None,
    kind: str | None = None,
) -> dict | None:
    if kind is not None and kind not in ANNOTATION_KINDS:
        raise ValueError(f"unsupported annotation kind: {kind}")
    annotations = load_annotations(arxiv_id)
    updated: dict | None = None
    for item in annotations:
        if item.get("id") != annotation_id:
            continue
        if note is not None:
            item["note"] = note
        if kind is not None:
            item["kind"] = kind
            item["color"] = ANNOTATION_KIND_COLORS[kind]
        item["updated_at"] = now_iso()
        updated = item
        break
    if updated is None:
        return None
    save_annotations(arxiv_id, annotations)
    return updated


def delete_annotation(arxiv_id: str, annotation_id: str) -> bool:
    annotations = load_annotations(arxiv_id)
    kept = [a for a in annotations if a.get("id") != annotation_id]
    if len(kept) == len(annotations):
        return False
    save_annotations(arxiv_id, kept)
    return True


def _paper_note_revision(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def load_paper_note(arxiv_id: str) -> dict:
    path = paper_dir(arxiv_id) / PAPER_NOTE_FILENAME
    markdown = path.read_text(encoding="utf-8") if path.exists() else ""
    updated_at = None
    if path.exists():
        updated_at = datetime.fromtimestamp(
            path.stat().st_mtime,
            timezone.utc,
        ).isoformat()
    return {
        "arxiv_id": arxiv_id,
        "markdown": markdown,
        "updated_at": updated_at,
        "revision": _paper_note_revision(markdown),
    }


def save_paper_note(arxiv_id: str, markdown: str, base_revision: str) -> dict:
    current = load_paper_note(arxiv_id)
    if base_revision != current["revision"]:
        raise PaperNoteRevisionConflict(current["revision"])
    path = ensure_paper_dir(arxiv_id) / PAPER_NOTE_FILENAME
    _atomic_write_text(path, markdown)
    return load_paper_note(arxiv_id)


def build_paper_note_summary(arxiv_id: str) -> dict:
    annotations = load_annotations(arxiv_id)
    paper_note = load_paper_note(arxiv_id)
    kind_counts = {kind: 0 for kind in sorted(ANNOTATION_KINDS)}
    anchors: list[dict] = []
    updated_values: list[str] = []
    for item in annotations:
        kind = item["kind"]
        kind_counts[kind] += 1
        if item.get("updated_at"):
            updated_values.append(item["updated_at"])
        if not str(item.get("note", "")).strip():
            continue
        selector = item.get("selector") if isinstance(item.get("selector"), dict) else {}
        anchors.append(
            {
                "annotation_id": item.get("id"),
                "kind": kind,
                "page": selector.get("page"),
                "block_index": item.get("block_index"),
                "region_id": selector.get("region_id"),
            }
        )
    if paper_note["updated_at"]:
        updated_values.append(paper_note["updated_at"])
    markdown_preview = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s+",
        "",
        paper_note["markdown"],
    )
    markdown_preview = re.sub(r"[`*_>~-]+", " ", markdown_preview)
    markdown_preview = " ".join(markdown_preview.split())
    selection_preview = next(
        (
            str(item.get("note") or "").strip()
            for item in reversed(annotations)
            if str(item.get("note") or "").strip()
        ),
        "",
    )
    return {
        "annotation_count": len(annotations),
        "selection_note_count": len(anchors),
        "has_paper_note": bool(paper_note["markdown"].strip()),
        "paper_note_revision": paper_note["revision"],
        "kind_counts": kind_counts,
        "updated_at": max(updated_values) if updated_values else None,
        "preview": (markdown_preview or selection_preview)[:180],
        "anchors": anchors[-100:],
    }


def save_collection_agent_report(collection_id: int, report: dict) -> None:
    d = ensure_collection_dir(collection_id)
    _write_json(d / "agent_report.json", report)


def load_collection_agent_report(collection_id: int) -> dict | None:
    path = collection_dir(collection_id) / "agent_report.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()
