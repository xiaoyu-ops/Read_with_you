"""Versioned, portable paper bundles for the opt-in local folder mode."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from ..extraction.blocks import PaperDocument
from . import files


PORTABLE_BUNDLE_VERSION = 1
PORTABLE_MAX_FILES = 2048
PORTABLE_MAX_FILE_BYTES = 220 * 1024 * 1024
PORTABLE_MAX_TOTAL_BYTES = 512 * 1024 * 1024
PORTABLE_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
PORTABLE_MANIFEST_HISTORY = 8

_ROOT_FILES = {
    "analysis.json",
    "annotations.json",
    "block_to_pdf_map.json",
    "extraction_quality.json",
    "mineru_content_list.json",
    "mineru_layout_meta.json",
    "mineru_middle.json",
    "mineru_source_meta.json",
    "original.md",
    "original.pdf",
    "paper_note.md",
    "translation.json",
    "translation_layout.json",
}
_ASSET_EXTENSIONS = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
}
_DERIVED_CACHE_DIRS = {
    "pdf_pages",
}


class PortableBundleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PortableSource:
    path: str
    file_path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class PortableExport:
    manifest: dict[str, Any]
    sources: tuple[PortableSource, ...]


def safe_paper_directory(papers_root: Path, paper_id: str) -> Path:
    value = str(paper_id or "").strip()
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PortableBundleError("portable_invalid_paper_id", "论文 ID 不合法。")
    root = papers_root.resolve()
    candidate = (root / Path(*path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PortableBundleError(
            "portable_invalid_paper_id",
            "论文 ID 超出文献目录。",
        ) from exc
    return candidate


def validate_portable_path(value: str) -> str:
    path = PurePosixPath(str(value or ""))
    if (
        not str(value)
        or path.is_absolute()
        or "\\" in str(value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PortableBundleError("portable_invalid_path", "可移植包包含非法路径。")
    normalized = path.as_posix()
    if normalized == "agent/chat.json":
        return normalized
    if len(path.parts) < 2 or path.parts[0] != "paper":
        raise PortableBundleError(
            "portable_path_not_allowed",
            f"可移植包路径不在允许范围内：{normalized}",
        )
    relative = PurePosixPath(*path.parts[1:])
    if len(relative.parts) == 1 and relative.name in _ROOT_FILES:
        return normalized
    if relative.parts[0] == "assets":
        if relative.suffix.lower() in _ASSET_EXTENSIONS and not any(
            part.startswith(".") for part in relative.parts
        ):
            return normalized
    if (
        relative.parts[0] == "mineru_layout_generations"
        and relative.suffix.lower() == ".json"
        and not any(part.startswith(".") for part in relative.parts)
    ):
        return normalized
    raise PortableBundleError(
        "portable_path_not_allowed",
        f"可移植包路径不在允许范围内：{normalized}",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _paper_sources(paper_root: Path) -> Iterator[tuple[str, Path]]:
    if not paper_root.is_dir():
        return
    for path in sorted(paper_root.rglob("*")):
        if path.is_symlink():
            raise PortableBundleError(
                "portable_symlink_rejected",
                "可移植包不接受符号链接。",
            )
        relative_path = path.relative_to(paper_root)
        if relative_path.parts[0] in _DERIVED_CACHE_DIRS:
            continue
        if not path.is_file():
            continue
        relative = relative_path.as_posix()
        portable_path = validate_portable_path(f"paper/{relative}")
        yield portable_path, path


def _safe_agent_id(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_") or "unknown"


def _agent_chat_path(agent_root: Path, paper_id: str) -> Path:
    return agent_root / "chats" / f"{_safe_agent_id(paper_id)}.json"


def _manifest_revision(
    paper_id: str,
    paper: dict[str, Any],
    file_entries: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "version": PORTABLE_BUNDLE_VERSION,
            "paper_id": paper_id,
            "paper": paper,
            "files": file_entries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _manifest_cache_dir(cache_root: Path, paper_id: str) -> Path:
    return cache_root / _safe_agent_id(paper_id)


def _load_manifest_history(
    cache_root: Path,
    paper_id: str,
    revision: str,
) -> dict[str, Any] | None:
    if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
        raise PortableBundleError(
            "portable_invalid_revision",
            "base_revision 必须是 64 位 SHA-256。",
        )
    path = _manifest_cache_dir(cache_root, paper_id) / f"{revision}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_manifest_history(
    cache_root: Path,
    paper_id: str,
    manifest: dict[str, Any],
) -> None:
    directory = _manifest_cache_dir(cache_root, paper_id)
    directory.mkdir(parents=True, exist_ok=True)
    files._write_json(directory / f"{manifest['revision']}.json", manifest)
    snapshots = sorted(
        directory.glob("*.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in snapshots[PORTABLE_MANIFEST_HISTORY:]:
        stale.unlink(missing_ok=True)


def build_portable_export(
    paper_id: str,
    paper_metadata: dict[str, Any],
    *,
    base_revision: str | None = None,
    papers_root: Path | None = None,
    agent_root: Path | None = None,
    cache_root: Path | None = None,
) -> PortableExport:
    papers_root = papers_root or files.PAPERS_DIR
    agent_root = agent_root or (files.DATA_DIR / "agent_workspace")
    cache_root = cache_root or (files.DATA_DIR / "portable_manifest_cache")
    paper_root = safe_paper_directory(papers_root, paper_id)
    if not (paper_root / "translation.json").is_file():
        raise PortableBundleError("portable_paper_missing", "论文文档不存在。")
    if not (paper_root / "original.pdf").is_file():
        raise PortableBundleError(
            "source_pdf_missing",
            "缺少原始 PDF，请重新导入后再保存到本地。",
        )

    raw_sources = list(_paper_sources(paper_root))
    chat_path = _agent_chat_path(agent_root, paper_id)
    if chat_path.is_file():
        raw_sources.append(("agent/chat.json", chat_path))
    if len(raw_sources) > PORTABLE_MAX_FILES:
        raise PortableBundleError("portable_too_many_files", "可移植包文件数量过多。")

    sources: list[PortableSource] = []
    total_size = 0
    for portable_path, source_path in raw_sources:
        size = source_path.stat().st_size
        if size > PORTABLE_MAX_FILE_BYTES:
            raise PortableBundleError(
                "portable_file_too_large",
                f"文件超过单文件限制：{portable_path}",
            )
        total_size += size
        if total_size > PORTABLE_MAX_TOTAL_BYTES:
            raise PortableBundleError("portable_bundle_too_large", "可移植包超过 512 MiB。")
        sources.append(
            PortableSource(
                path=portable_path,
                file_path=source_path,
                size=size,
                sha256=_sha256_file(source_path),
            )
        )

    file_entries = [
        {"path": source.path, "size": source.size, "sha256": source.sha256}
        for source in sorted(sources, key=lambda item: item.path)
    ]
    paper = {
        "title": str(paper_metadata.get("title") or "")[:1000],
        "authors": [
            str(author)[:300]
            for author in (paper_metadata.get("authors") or [])
            if str(author).strip()
        ][:200],
        "source": str(paper_metadata.get("source") or "portable")[:80],
        "status": str(paper_metadata.get("status") or "extracted")[:80],
    }
    revision = _manifest_revision(paper_id, paper, file_entries)
    current = {
        "version": PORTABLE_BUNDLE_VERSION,
        "paper_id": paper_id,
        "paper": paper,
        "revision": revision,
        "files": file_entries,
    }

    previous = (
        _load_manifest_history(cache_root, paper_id, base_revision)
        if base_revision
        else None
    )
    current_by_path = {entry["path"]: entry for entry in file_entries}
    previous_by_path = {
        str(entry.get("path")): entry
        for entry in (previous or {}).get("files", [])
        if isinstance(entry, dict)
    }
    if base_revision and previous is not None:
        included_paths = [
            path
            for path, entry in current_by_path.items()
            if previous_by_path.get(path) != entry
        ]
        deleted_paths = sorted(set(previous_by_path) - set(current_by_path))
        bundle_type = "delta"
    else:
        included_paths = sorted(current_by_path)
        deleted_paths = []
        bundle_type = "full"

    manifest = {
        **current,
        "bundle_type": bundle_type,
        "base_revision": base_revision,
        "included_paths": included_paths,
        "deleted_paths": deleted_paths,
        "total_bytes": sum(int(entry["size"]) for entry in file_entries),
    }
    _save_manifest_history(cache_root, paper_id, current)
    source_by_path = {source.path: source for source in sources}
    return PortableExport(
        manifest=manifest,
        sources=tuple(source_by_path[path] for path in included_paths),
    )


def parse_portable_manifest(raw: bytes) -> dict[str, Any]:
    if len(raw) > PORTABLE_MAX_MANIFEST_BYTES:
        raise PortableBundleError("portable_manifest_too_large", "manifest 过大。")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableBundleError("portable_manifest_invalid", "manifest 不是合法 JSON。") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != PORTABLE_BUNDLE_VERSION:
        raise PortableBundleError(
            "portable_manifest_version",
            "不支持的可移植包版本。",
        )
    paper_id = str(manifest.get("paper_id") or "")
    safe_paper_directory(Path("/tmp/portable-paper-id-check"), paper_id)
    files_data = manifest.get("files")
    if not isinstance(files_data, list) or not files_data:
        raise PortableBundleError("portable_manifest_files", "manifest 缺少文件清单。")
    if len(files_data) > PORTABLE_MAX_FILES:
        raise PortableBundleError("portable_too_many_files", "可移植包文件数量过多。")
    paper = manifest.get("paper")
    if not isinstance(paper, dict):
        raise PortableBundleError("portable_manifest_paper", "manifest 缺少论文元数据。")
    title = str(paper.get("title") or "").strip()
    authors = paper.get("authors")
    if not title or len(title) > 1000 or not isinstance(authors, list):
        raise PortableBundleError("portable_manifest_paper", "论文元数据格式错误。")
    normalized_paper = {
        "title": title,
        "authors": [
            str(author)[:300]
            for author in authors
            if isinstance(author, str) and author.strip()
        ][:200],
        "source": str(paper.get("source") or "portable")[:80],
        "status": str(paper.get("status") or "extracted")[:80],
    }
    seen: set[str] = set()
    total = 0
    for entry in files_data:
        if not isinstance(entry, dict):
            raise PortableBundleError("portable_manifest_files", "文件清单格式错误。")
        path = validate_portable_path(str(entry.get("path") or ""))
        if path in seen:
            raise PortableBundleError("portable_duplicate_path", "可移植包含重复路径。")
        seen.add(path)
        size = entry.get("size")
        digest = str(entry.get("sha256") or "")
        if not isinstance(size, int) or size < 0 or size > PORTABLE_MAX_FILE_BYTES:
            raise PortableBundleError("portable_invalid_size", f"文件大小非法：{path}")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise PortableBundleError("portable_invalid_hash", f"文件哈希非法：{path}")
        total += size
        if total > PORTABLE_MAX_TOTAL_BYTES:
            raise PortableBundleError("portable_bundle_too_large", "可移植包超过 512 MiB。")
    included = manifest.get("included_paths")
    if (
        not isinstance(included, list)
        or len(included) != len(seen)
        or set(map(str, included)) != seen
    ):
        raise PortableBundleError(
            "portable_incomplete_restore",
            "恢复包必须包含 manifest 中的全部文件。",
        )
    if manifest.get("bundle_type") != "full":
        raise PortableBundleError(
            "portable_restore_requires_full",
            "恢复服务端必须提交完整本地包。",
        )
    normalized_entries = sorted(
        (
            {
                "path": validate_portable_path(str(entry["path"])),
                "size": int(entry["size"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in files_data
        ),
        key=lambda entry: entry["path"],
    )
    computed_revision = _manifest_revision(paper_id, normalized_paper, normalized_entries)
    provided_revision = str(manifest.get("revision") or "")
    if provided_revision and provided_revision != computed_revision:
        raise PortableBundleError(
            "portable_revision_mismatch",
            "manifest revision 与文件清单不一致。",
        )
    base_revision = str(manifest.get("base_revision") or "")
    if base_revision and (
        len(base_revision) != 64
        or any(char not in "0123456789abcdef" for char in base_revision)
    ):
        raise PortableBundleError(
            "portable_invalid_revision",
            "base_revision 必须是 64 位 SHA-256。",
        )
    manifest["paper"] = normalized_paper
    manifest["files"] = normalized_entries
    manifest["included_paths"] = [entry["path"] for entry in normalized_entries]
    manifest["revision"] = computed_revision
    manifest["base_revision"] = base_revision or None
    return manifest


def stage_portable_files(
    manifest: dict[str, Any],
    streams: list[BinaryIO],
    *,
    staging_parent: Path,
) -> Path:
    entries = manifest["files"]
    if len(streams) != len(entries):
        raise PortableBundleError(
            "portable_file_count_mismatch",
            "上传文件数量与 manifest 不一致。",
        )
    stage_root = Path(tempfile.mkdtemp(prefix=".portable-", dir=staging_parent))
    try:
        for entry, stream in zip(entries, streams, strict=True):
            portable_path = validate_portable_path(str(entry["path"]))
            relative = PurePosixPath(portable_path)
            target = stage_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with target.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > int(entry["size"]) or size > PORTABLE_MAX_FILE_BYTES:
                        raise PortableBundleError(
                            "portable_size_mismatch",
                            f"文件大小不匹配：{portable_path}",
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != int(entry["size"]) or digest.hexdigest() != entry["sha256"]:
                raise PortableBundleError(
                    "portable_hash_mismatch",
                    f"文件校验失败：{portable_path}",
                )
        translation_path = stage_root / "paper" / "translation.json"
        try:
            document = PaperDocument.from_dict(
                json.loads(translation_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PortableBundleError(
                "portable_document_invalid",
                "translation.json 无法恢复为论文文档。",
            ) from exc
        if document.paper_id != manifest["paper_id"]:
            raise PortableBundleError(
                "portable_paper_id_mismatch",
                "manifest 与论文文档 ID 不一致。",
            )
        chat_path = stage_root / "agent" / "chat.json"
        if chat_path.is_file():
            try:
                chat = json.loads(chat_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PortableBundleError(
                    "portable_chat_invalid",
                    "对话文件不是合法 JSON。",
                ) from exc
            messages = chat.get("messages") if isinstance(chat, dict) else None
            if (
                not isinstance(chat, dict)
                or str(chat.get("arxiv_id") or "") != manifest["paper_id"]
                or not isinstance(messages, list)
                or len(messages) > 200
                or any(not isinstance(message, dict) for message in messages)
            ):
                raise PortableBundleError(
                    "portable_chat_invalid",
                    "对话文件格式错误。",
                )
        return stage_root
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def apply_staged_portable_bundle(
    manifest: dict[str, Any],
    stage_root: Path,
    *,
    papers_root: Path | None = None,
    agent_root: Path | None = None,
) -> None:
    papers_root = papers_root or files.PAPERS_DIR
    agent_root = agent_root or (files.DATA_DIR / "agent_workspace")
    paper_id = str(manifest["paper_id"])
    target = safe_paper_directory(papers_root, paper_id)
    staged_paper = stage_root / "paper"
    backup = target.parent / f".{target.name}.portable-backup-{uuid4().hex}"
    chat_target = _agent_chat_path(agent_root, paper_id)
    staged_chat = stage_root / "agent" / "chat.json"
    old_chat = chat_target.read_bytes() if chat_target.is_file() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    replaced = False
    try:
        if target.exists():
            os.replace(target, backup)
        os.replace(staged_paper, target)
        replaced = True
        if staged_chat.is_file():
            chat_target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(chat_target, staged_chat.read_bytes())
        else:
            chat_target.unlink(missing_ok=True)
    except Exception:
        if replaced and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            os.replace(backup, target)
        if old_chat is None:
            chat_target.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(chat_target, old_chat)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(stage_root, ignore_errors=True)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
