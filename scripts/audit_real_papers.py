"""Read-only regression gate for the fixed T9 real-paper samples.

Usage:
  python scripts/audit_real_papers.py
  python scripts/audit_real_papers.py --papers-dir /path/to/data/papers --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.extraction.blocks import PaperDocument  # noqa: E402
from backend.extraction.quality import assess_extraction_quality  # noqa: E402


PAPERS_DIR = ROOT / "data" / "papers"
SAMPLE_GATES = {
    "1706.03762": {"source": "ar5iv", "min_blocks": 90, "min_mapped_ratio": 0.95},
    "2303.09540": {"source": "ar5iv", "min_blocks": 140, "min_mapped_ratio": 0.90},
    "2104.08691": {"source": "ar5iv", "min_blocks": 95, "min_mapped_ratio": 0.82},
    "2512.24957": {"source": "latex", "min_blocks": 100, "min_mapped_ratio": 0.30},
}
MIN_AVERAGE_CONFIDENCE = 0.85


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def audit_paper(papers_dir: Path, paper_id: str, gate: dict) -> dict:
    paper_dir = papers_dir / paper_id
    document_path = paper_dir / "translation.json"
    mapping_path = paper_dir / "block_to_pdf_map.json"
    result = {
        "paper_id": paper_id,
        "status": "fail",
        "source": None,
        "block_count": 0,
        "block_types": {},
        "quality_acceptable": False,
        "quality_findings": [],
        "pdf_pages": None,
        "mapped_ratio": None,
        "average_confidence": None,
        "low_confidence_count": None,
        "reasons": [],
    }

    if not document_path.exists():
        result["status"] = "skipped"
        result["reasons"].append("document_missing")
        return result

    try:
        doc = PaperDocument.from_dict(_load_json(document_path))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result["reasons"].append(f"document_unreadable:{exc}")
        return result

    quality = assess_extraction_quality(doc.blocks, doc.source)
    result.update(
        source=doc.source,
        block_count=len(doc.blocks),
        block_types=quality.type_counts,
        quality_acceptable=quality.acceptable,
        quality_findings=[item.code for item in quality.findings],
    )
    if doc.source != gate["source"]:
        result["reasons"].append(f"source:{doc.source}!={gate['source']}")
    if len(doc.blocks) < gate["min_blocks"]:
        result["reasons"].append(f"blocks:{len(doc.blocks)}<{gate['min_blocks']}")
    if not quality.acceptable:
        result["reasons"].append("extraction_quality_unacceptable")

    if not mapping_path.exists():
        result["status"] = "skipped"
        result["reasons"].append("pdf_mapping_missing")
        return result

    try:
        mapping = _load_json(mapping_path)
        page_count = int(mapping.get("page_count") or 0)
        mapped_ratio = float(mapping.get("mapped_ratio") or 0)
        average_confidence = float(mapping.get("average_confidence") or 0)
        low_confidence_count = int(mapping.get("low_confidence_count") or 0)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result["reasons"].append(f"pdf_mapping_unreadable:{exc}")
        return result

    result.update(
        pdf_pages=page_count,
        mapped_ratio=round(mapped_ratio, 3),
        average_confidence=round(average_confidence, 3),
        low_confidence_count=low_confidence_count,
    )
    if page_count <= 0:
        result["status"] = "skipped"
        result["reasons"].append("pdf_not_applicable")
        return result
    if mapped_ratio < gate["min_mapped_ratio"]:
        result["reasons"].append(f"mapped_ratio:{mapped_ratio:.3f}<{gate['min_mapped_ratio']:.3f}")
    if average_confidence < MIN_AVERAGE_CONFIDENCE:
        result["reasons"].append(
            f"average_confidence:{average_confidence:.3f}<{MIN_AVERAGE_CONFIDENCE:.3f}"
        )

    result["status"] = "pass" if not result["reasons"] else "fail"
    return result


def audit_papers(papers_dir: Path = PAPERS_DIR) -> list[dict]:
    return [audit_paper(papers_dir, paper_id, gate) for paper_id, gate in SAMPLE_GATES.items()]


def _format_result(result: dict) -> str:
    types = ",".join(f"{key}:{value}" for key, value in sorted(result["block_types"].items())) or "-"
    findings = ",".join(result["quality_findings"]) or "none"
    reasons = ",".join(result["reasons"]) or "none"
    ratio = "-" if result["mapped_ratio"] is None else f"{result['mapped_ratio']:.3f}"
    confidence = (
        "-" if result["average_confidence"] is None else f"{result['average_confidence']:.3f}"
    )
    return (
        f"{result['paper_id']} {result['status'].upper()} source={result['source'] or '-'} "
        f"blocks={result['block_count']} types={types} quality={result['quality_acceptable']} "
        f"findings={findings} pdf_pages={result['pdf_pages'] or '-'} mapped_ratio={ratio} "
        f"avg_conf={confidence} low_conf={result['low_confidence_count']} reasons={reasons}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the fixed T9 real-paper regression samples.")
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    results = audit_papers(args.papers_dir)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(_format_result(result))
    return 0 if all(result["status"] == "pass" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
