"""Read-only audit for the original-position translation layout contract.

Existing papers without ``translation_layout.json`` are converted from the
legacy PDF map in memory for diagnostics only and fail the precise-layout gate.
The script never mutates paper data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.extraction.blocks import PaperDocument  # noqa: E402
from backend.extraction.mineru import (  # noqa: E402
    MINERU_LAYOUT_ADAPTER,
    MINERU_LAYOUT_ADAPTER_VERSION,
)
from backend.extraction.pdf_layout import (  # noqa: E402
    POPPLER_LAYOUT_ADAPTER,
    PdfLayoutError,
    extract_pdf_layout,
)
from backend.extraction.translation_layout import (  # noqa: E402
    HYBRID_LAYOUT_ADAPTER,
    TranslationLayout,
    legacy_latex_extraction_debris_indexes,
    mappable_text_block_indexes,
    safe_translation_layout_metrics,
    translation_layout_cache_matches,
    translation_layout_from_pdf_layout,
    translation_layout_from_pdf_map,
)
from backend.storage.files import (  # noqa: E402
    load_mineru_layout_artifact_bundle_from_dir,
)


PAPERS_DIR = ROOT / "data" / "papers"
DEFAULT_PAPERS = ("1706.03762", "2303.09540", "2104.08691", "2512.24957")
PRECISE_ADAPTERS = {
    POPPLER_LAYOUT_ADAPTER,
    MINERU_LAYOUT_ADAPTER,
    HYBRID_LAYOUT_ADAPTER,
}
PRECISE_POPPLER_MINIMUM = 0.90
SAFE_REPLACE_AVERAGE_MINIMUM = 0.92
SOURCE_CLASS_THRESHOLDS = {
    "arxiv_digital": 0.90,
    "local_digital": 0.85,
    "mineru_complex": 0.80,
    "scan_ocr": 0.70,
}
_ARXIV_SOURCES = {"arxiv", "ar5iv", "latex"}
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")
_TRANSLATABLE_TEXT_KINDS = {
    "heading",
    "paragraph",
    "text",
    "title",
    "list",
    "page_footnote",
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
    "table_caption",
    "table_footnote",
    "code_caption",
    "code_footnote",
}
_MINERU_DIRECT_TEXT_KINDS = {"text", "title", "list", "page_footnote"}
_MINERU_REFERENCE_KINDS = {"ref_text"}
_MINERU_PROTECTED_BODY_KINDS = {
    "image",
    "chart",
    "table",
    "code",
    "equation",
    "algorithm",
}
_MINERU_CAPTION_FIELDS = tuple(
    f"{kind}_{suffix}"
    for kind in ("image", "chart", "table", "code")
    for suffix in ("caption", "footnote")
)


def _load_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def _safe_layout_metrics(document: PaperDocument, layout: TranslationLayout) -> dict:
    return safe_translation_layout_metrics(document.blocks, layout)


def _has_content_text(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(part, str) and part.strip() for part in value)
    return False


def _mineru_source_page_evidence(
    directory: Path,
    document: PaperDocument,
    layout: TranslationLayout,
) -> dict:
    """Compare bound MinerU source-text pages with reachable layout regions."""
    unavailable = {
        "source_text_page_evidence": "unavailable",
        "source_text_page_count": 0,
        "accessible_text_page_count": 0,
        "missing_text_pages": [],
        "reference_only_pages": [],
        "protected_only_pages": [],
    }
    mineru_source = next(
        (source for source in layout.sources if source.adapter == MINERU_LAYOUT_ADAPTER),
        None,
    )
    if mineru_source is None or mineru_source.generation is None:
        return unavailable
    bundle = load_mineru_layout_artifact_bundle_from_dir(
        directory,
        expected_source_pdf_sha256=layout.source_pdf_sha256,
    )
    if (
        bundle is None
        or bundle[2].get("generation") != mineru_source.generation
        or bundle[2].get("is_ocr") != mineru_source.is_ocr
    ):
        return unavailable

    content_list = bundle[1]
    expected_text_pages: set[int] = set()
    reference_pages: set[int] = set()
    protected_body_pages: set[int] = set()
    for item in content_list:
        page_index = item.get("page_idx")
        if (
            not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < 0
            or page_index >= layout.page_count
        ):
            return unavailable
        page = page_index + 1
        kind = str(item.get("type") or "").strip().lower()
        if kind in _MINERU_DIRECT_TEXT_KINDS:
            direct_value = item.get("list_items") if kind == "list" else item.get("text")
            if not _has_content_text(direct_value):
                direct_value = item.get("content")
            if _has_content_text(direct_value):
                expected_text_pages.add(page)
        if kind in _MINERU_REFERENCE_KINDS and _has_content_text(
            item.get("text") or item.get("content")
        ):
            reference_pages.add(page)
        if kind in _MINERU_PROTECTED_BODY_KINDS:
            protected_body_pages.add(page)
        if any(_has_content_text(item.get(field)) for field in _MINERU_CAPTION_FIELDS):
            expected_text_pages.add(page)

    mappable_indexes = mappable_text_block_indexes(document.blocks)
    accessible_pages = {
        region.page
        for region in layout.regions
        if region.block_index in mappable_indexes
        and region.kind in _TRANSLATABLE_TEXT_KINDS
        and region.render_policy in {"replace", "panel_only"}
    }
    reachable_expected_pages = expected_text_pages & accessible_pages
    reference_only_pages = reference_pages - expected_text_pages
    protected_only_pages = (
        protected_body_pages - expected_text_pages - reference_only_pages
    )
    missing_text_pages = expected_text_pages - accessible_pages
    accessible_count = len(reachable_expected_pages)
    return {
        "source_text_page_evidence": "mineru_content_list",
        "source_text_page_count": len(expected_text_pages),
        "accessible_text_page_count": accessible_count,
        "missing_text_pages": sorted(missing_text_pages),
        "reference_only_pages": sorted(reference_only_pages),
        "protected_only_pages": sorted(protected_only_pages),
    }


def _mineru_ocr_provenance(
    directory: Path,
    layout: TranslationLayout,
) -> bool | None:
    values: list[tuple[bool, str | None]] = []
    specifications = (
        ("mineru_layout_meta.json", 2, True),
        ("mineru_source_meta.json", 1, False),
    )
    for name, schema_version, generation_required in specifications:
        path = directory / name
        if not path.exists():
            continue
        try:
            meta = _load_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        generation = meta.get("generation")
        valid_generation = (
            isinstance(generation, str)
            and len(generation) == 32
            and all(character in "0123456789abcdef" for character in generation)
        )
        if (
            meta.get("schema_version") != schema_version
            or meta.get("adapter") != MINERU_LAYOUT_ADAPTER
            or meta.get("adapter_version") != MINERU_LAYOUT_ADAPTER_VERSION
            or meta.get("source_pdf_sha256") != layout.source_pdf_sha256
            or not isinstance(meta.get("is_ocr"), bool)
            or (generation_required and not valid_generation)
            or (generation is not None and not valid_generation)
        ):
            return None
        values.append((meta["is_ocr"], generation))
    if not values:
        return None
    if any(value[0] != values[0][0] for value in values[1:]):
        return None
    known_generations = {generation for _, generation in values if generation is not None}
    if len(known_generations) > 1:
        return None
    layout_source = next(
        (source for source in layout.sources if source.adapter == MINERU_LAYOUT_ADAPTER),
        None,
    )
    if (
        layout_source is None
        or layout_source.generation is None
        or known_generations != {layout_source.generation}
        or layout_source.is_ocr != values[0][0]
    ):
        return None
    return values[0][0]


def _source_class(
    directory: Path,
    paper_id: str,
    document: PaperDocument,
    layout: TranslationLayout,
) -> str | None:
    if layout.adapter == POPPLER_LAYOUT_ADAPTER:
        arxiv_identifier = _ARXIV_ID_RE.fullmatch(paper_id) is not None
        if document.source in _ARXIV_SOURCES and arxiv_identifier:
            return "arxiv_digital"
        if document.source == "local_pdf" and not arxiv_identifier:
            return "local_digital"
        return None
    if layout.adapter in {MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}:
        is_ocr = _mineru_ocr_provenance(directory, layout)
        if is_ocr is True:
            return "scan_ocr"
        if is_ocr is False:
            return "mineru_complex"
    return None


def audit_paper(
    papers_dir: Path,
    paper_id: str,
    *,
    probe_poppler: bool = False,
) -> dict:
    directory = papers_dir / paper_id
    result = {
        "paper_id": paper_id,
        "status": "fail",
        "source": None,
        "layout_source": None,
        "adapter": None,
        "page_count": 0,
        "region_count": 0,
        "mapped_ratio": 0.0,
        "average_confidence": 0.0,
        "replaceable_count": 0,
        "panel_only_count": 0,
        "protected_count": 0,
        "protected_overlap_count": 0,
        "legacy_latex_debris_count": 0,
        "source_text_page_evidence": "unavailable",
        "source_text_page_count": 0,
        "accessible_text_page_count": 0,
        "missing_text_pages": [],
        "reference_only_pages": [],
        "protected_only_pages": [],
        "eligible_count": 0,
        "protected_excluded_count": 0,
        "safe_replace_count": 0,
        "safe_coverage": 0.0,
        "replace_average_confidence": 0.0,
        "source_class": None,
        "threshold": None,
        "failure_counts": {},
        "warnings": [],
        "reasons": [],
    }
    try:
        document = PaperDocument.from_dict(_load_object(directory / "translation.json"))
        legacy_latex_debris = (
            legacy_latex_extraction_debris_indexes(document.blocks)
            if document.source == "latex"
            else set()
        )
        result["legacy_latex_debris_count"] = len(legacy_latex_debris)
        if legacy_latex_debris:
            result["reasons"].append("legacy_latex_extraction_debris")
        pdf_path = directory / "original.pdf"
        if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
            raise FileNotFoundError("source_pdf_missing")

        layout_path = directory / "translation_layout.json"
        if layout_path.exists():
            layout_data = _load_object(layout_path)
            layout = TranslationLayout.model_validate(layout_data)
            if not translation_layout_cache_matches(layout_data, document.blocks, pdf_path):
                result["reasons"].append("cache_fingerprint_mismatch")
            layout_source = "cache"
        elif probe_poppler:
            try:
                poppler_document = extract_pdf_layout(pdf_path)
                layout = translation_layout_from_pdf_layout(
                    document.blocks,
                    pdf_path,
                    poppler_document,
                )
                layout_source = "poppler_probe"
                if (
                    layout.quality.mapped_ratio < PRECISE_POPPLER_MINIMUM
                    or layout.quality.average_confidence < PRECISE_POPPLER_MINIMUM
                ):
                    result["reasons"].append("mineru_fallback_required")
            except (OSError, PdfLayoutError, ValueError) as exc:
                mapping = _load_object(directory / "block_to_pdf_map.json")
                layout = translation_layout_from_pdf_map(document.blocks, pdf_path, mapping)
                layout_source = "legacy_preview"
                result["reasons"].append(f"poppler_probe_failed:{exc}")
        else:
            mapping = _load_object(directory / "block_to_pdf_map.json")
            layout = translation_layout_from_pdf_map(document.blocks, pdf_path, mapping)
            layout_source = "legacy_preview"
            result["reasons"].append("precise_layout_missing")

        if (
            layout.adapter not in PRECISE_ADAPTERS
            and "precise_layout_missing" not in result["reasons"]
        ):
            result["reasons"].append("precise_adapter_required")

        ordering = [
            (region.block_index, region.page, region.flow_order) for region in layout.regions
        ]
        if ordering != sorted(ordering):
            result["reasons"].append("region_order_invalid")

        quality = layout.quality
        safe_metrics = _safe_layout_metrics(document, layout)
        source_page_evidence = _mineru_source_page_evidence(
            directory,
            document,
            layout,
        )
        if source_page_evidence["source_text_page_evidence"] == "unavailable":
            if layout.adapter in {MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}:
                result["reasons"].append("source_text_page_evidence_unavailable")
        elif source_page_evidence["missing_text_pages"]:
            result["reasons"].append("source_text_page_without_region")
        source_class = _source_class(directory, paper_id, document, layout)
        threshold = SOURCE_CLASS_THRESHOLDS.get(source_class) if source_class else None
        if source_class is None:
            result["reasons"].append("source_class_unknown")
        else:
            if safe_metrics["safe_coverage"] < threshold:
                result["reasons"].append("safe_coverage_below_threshold")
            if (
                safe_metrics["replace_average_confidence"]
                < SAFE_REPLACE_AVERAGE_MINIMUM
            ):
                result["reasons"].append(
                    "replace_average_confidence_below_threshold"
                )

        warnings = list(layout.warnings)
        if source_page_evidence["source_text_page_evidence"] == "unavailable":
            warnings.append("source_text_page_evidence_unavailable")
        result.update(
            source=document.source,
            layout_source=layout_source,
            adapter=layout.adapter,
            page_count=layout.page_count,
            region_count=len(layout.regions),
            mapped_ratio=quality.mapped_ratio,
            average_confidence=quality.average_confidence,
            replaceable_count=quality.replaceable_count,
            panel_only_count=quality.panel_only_count,
            protected_count=quality.protected_count,
            protected_overlap_count=quality.protected_overlap_count,
            **source_page_evidence,
            **safe_metrics,
            source_class=source_class,
            threshold=threshold,
            failure_counts=quality.failure_counts,
            warnings=warnings,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["reasons"].append(str(exc))

    result["status"] = "pass" if not result["reasons"] else "fail"
    return result


def audit_papers(
    papers_dir: Path,
    paper_ids: list[str],
    *,
    probe_poppler: bool = False,
) -> list[dict]:
    return [
        audit_paper(papers_dir, paper_id, probe_poppler=probe_poppler)
        for paper_id in paper_ids
    ]


def _format_result(result: dict) -> str:
    failures = ",".join(
        f"{name}:{count}" for name, count in sorted(result["failure_counts"].items())
    ) or "none"
    warnings = ",".join(result["warnings"]) or "none"
    reasons = ",".join(result["reasons"]) or "none"
    return (
        f"{result['paper_id']} {result['status'].upper()} source={result['source'] or '-'} "
        f"layout={result['layout_source'] or '-'} adapter={result['adapter'] or '-'} "
        f"pages={result['page_count']} regions={result['region_count']} "
        f"mapped_ratio={result['mapped_ratio']:.3f} "
        f"avg_conf={result['average_confidence']:.3f} "
        f"source_class={result['source_class'] or '-'} "
        f"eligible={result['eligible_count']} "
        f"protected_excluded={result['protected_excluded_count']} "
        f"safe_replace={result['safe_replace_count']} "
        f"safe_coverage={result['safe_coverage']:.3f} "
        f"replace_avg_conf={result['replace_average_confidence']:.3f} "
        f"threshold={result['threshold'] if result['threshold'] is not None else '-'} "
        f"replace={result['replaceable_count']} panel={result['panel_only_count']} "
        f"protected={result['protected_count']} "
        f"protected_overlap={result['protected_overlap_count']} "
        f"source_text_pages={result['source_text_page_count']} "
        f"accessible_text_pages={result['accessible_text_page_count']} "
        f"missing_text_pages={result['missing_text_pages']} "
        f"failures={failures} warnings={warnings} reasons={reasons}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit original-position translation layouts.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--paper", action="append", dest="papers")
    parser.add_argument(
        "--probe-poppler",
        action="store_true",
        help="build a read-only Poppler layout in memory when no precise cache exists",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    paper_ids = args.papers or list(DEFAULT_PAPERS)

    results = audit_papers(
        args.papers_dir,
        paper_ids,
        probe_poppler=args.probe_poppler,
    )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(_format_result(result))
    return 0 if all(result["status"] == "pass" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
