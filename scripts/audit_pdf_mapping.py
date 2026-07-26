"""Audit cached PDF/block mapping quality.

Usage:
  python scripts/audit_pdf_mapping.py
  python scripts/audit_pdf_mapping.py --min-mapped-ratio 0.75 --min-average-confidence 0.82
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPERS_DIR = ROOT / "data" / "papers"


def load_maps() -> list[tuple[str, dict]]:
    items: list[tuple[str, dict]] = []
    if not PAPERS_DIR.exists():
        return items
    for path in sorted(PAPERS_DIR.glob("*/block_to_pdf_map.json")):
        try:
            items.append((path.parent.name, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            items.append((path.parent.name, {"error": str(exc)}))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit cached PDF/block mapping quality.")
    parser.add_argument("--min-mapped-ratio", type=float, default=0.65)
    parser.add_argument("--min-average-confidence", type=float, default=0.78)
    args = parser.parse_args()

    maps = load_maps()
    if not maps:
        print("No PDF mapping cache found under data/papers/*/block_to_pdf_map.json")
        return 0

    failed = False
    for arxiv_id, data in maps:
        if "error" in data:
            print(f"{arxiv_id}: unreadable mapping ({data['error']})")
            failed = True
            continue

        mapped_ratio = float(data.get("mapped_ratio") or 0)
        average_confidence = float(data.get("average_confidence") or 0)
        low_confidence_count = int(data.get("low_confidence_count") or 0)
        mapping_count = int(data.get("mapping_count") or 0)
        mappable_count = int(data.get("mappable_count") or 0)

        status = "ok"
        if (
            mapped_ratio < args.min_mapped_ratio
            or average_confidence < args.min_average_confidence
        ):
            status = "review"
            failed = True

        print(
            f"{arxiv_id}: {status} "
            f"mapped={mapping_count}/{mappable_count} "
            f"ratio={mapped_ratio:.0%} "
            f"avg_conf={average_confidence:.0%} "
            f"low_conf={low_confidence_count}"
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
