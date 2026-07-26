"""Resolve Agent evidence to the current versioned PDF translation layout.

Models may identify a stable ``block_index``.  Page and region coordinates are
always derived here from Pet's persisted layout so stale or invented geometry
never drives the reader.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_BLOCK_REFERENCE = re.compile(
    r"(?:\bblock\s*#?\s*|段落\s*#?\s*)(\d+)\b",
    re.IGNORECASE,
)


def enrich_result_data_locations(
    result_data: dict,
    layout: dict | None,
    *,
    valid_block_indexes: Iterable[int] | None = None,
) -> dict:
    """Return a copy whose evidence locators use the current layout only."""

    enriched = dict(result_data)
    raw_evidence = result_data.get("evidence")
    if not isinstance(raw_evidence, list):
        return enriched

    valid_indexes = (
        {index for index in valid_block_indexes if _non_negative_int(index) is not None}
        if valid_block_indexes is not None
        else None
    )
    primary_regions = _primary_regions_by_block(layout)
    regions_by_identity = _regions_by_identity(layout)
    evidence: list[dict] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        block_index = evidence_block_index(item)
        if block_index is None or (
            valid_indexes is not None and block_index not in valid_indexes
        ):
            # A model-supplied page/region without a valid block anchor is not
            # trustworthy.  Keep the human-readable evidence, drop geometry.
            item.pop("location", None)
            evidence.append(item)
            continue

        location: dict[str, object] = {"block_index": block_index}
        raw_location = item.get("location")
        preferred_region_id = (
            raw_location.get("region_id") if isinstance(raw_location, dict) else None
        )
        region = (
            regions_by_identity.get((block_index, preferred_region_id.strip()))
            if isinstance(preferred_region_id, str) and preferred_region_id.strip()
            else None
        ) or primary_regions.get(block_index)
        if region is not None:
            location["page"] = region["page"]
            location["region_id"] = region["region_id"]
        item["location"] = location
        evidence.append(item)

    enriched["evidence"] = evidence
    return enriched


def evidence_block_index(item: dict) -> int | None:
    """Read the compatible block anchors accepted by Pet evidence."""

    location = item.get("location")
    if isinstance(location, dict):
        value = _non_negative_int(location.get("block_index"))
        if value is not None:
            return value
    value = _non_negative_int(item.get("block_index"))
    if value is not None:
        return value
    for field in ("source", "citation"):
        text = item.get(field)
        if not isinstance(text, str):
            continue
        match = _BLOCK_REFERENCE.search(text)
        if match:
            return int(match.group(1))
    return None


def _primary_regions_by_block(layout: dict | None) -> dict[int, dict[str, object]]:
    regions = layout.get("regions") if isinstance(layout, dict) else None
    if not isinstance(regions, list):
        return {}
    grouped: dict[int, list[dict[str, object]]] = {}
    for raw_region in regions:
        if not isinstance(raw_region, dict):
            continue
        block_index = _non_negative_int(raw_region.get("block_index"))
        page = _positive_int(raw_region.get("page"))
        region_id = raw_region.get("region_id")
        if block_index is None or page is None or not isinstance(region_id, str) or not region_id.strip():
            continue
        grouped.setdefault(block_index, []).append(
            {
                "block_index": block_index,
                "page": page,
                "region_id": region_id.strip(),
                "flow_order": _non_negative_int(raw_region.get("flow_order")),
            }
        )
    return {
        block_index: min(
            candidates,
            key=lambda region: (
                region["flow_order"] if isinstance(region["flow_order"], int) else 2**31,
                region["page"],
                region["region_id"],
            ),
        )
        for block_index, candidates in grouped.items()
    }


def _regions_by_identity(layout: dict | None) -> dict[tuple[int, str], dict[str, object]]:
    regions = layout.get("regions") if isinstance(layout, dict) else None
    if not isinstance(regions, list):
        return {}
    result: dict[tuple[int, str], dict[str, object]] = {}
    for raw_region in regions:
        if not isinstance(raw_region, dict):
            continue
        block_index = _non_negative_int(raw_region.get("block_index"))
        page = _positive_int(raw_region.get("page"))
        region_id = raw_region.get("region_id")
        if block_index is None or page is None or not isinstance(region_id, str) or not region_id.strip():
            continue
        normalized_id = region_id.strip()
        result[(block_index, normalized_id)] = {
            "block_index": block_index,
            "page": page,
            "region_id": normalized_id,
        }
    return result


def _non_negative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
